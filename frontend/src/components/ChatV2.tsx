import { useEffect, useLayoutEffect, useRef, useState } from 'react';

import { apiService } from '../services/api';
import {
  AttachmentInfo,
  AskUserQuestion,
  ChatContentBlock,
  FileInfo,
  ModelInfo,
  RoundData,
  ToolApprovalPayload,
} from '../types';
import {
  ChatRuntimeProvider,
  useChatRuntime,
  useChatRuntimeOptional,
} from '../runtime/ChatRuntimeProvider';
import { readFileAsDataUrl } from '../utils/imageUtils';
import { toFileInfo, isImageFile } from '../utils/fileUtils';
import { extractAssistantFiles } from '../utils/assistantFileRefs';
import { restoreFailedSkillDraft, type SkillDraft } from '../utils/skillDrafts';
import {
  MAX_TEXT_BLOCK_CHARS,
  formatUploadError,
  messageTooLongText,
} from '../utils/errorMessages';
import { Round } from './Round';
import { ArtifactsPanel } from './ArtifactsPanel';
import { FilePreview } from './FilePreview';
import { ModelSelector } from './ModelSelector';
import { ChatInput } from './ChatInput';
import { QuestionCard } from './QuestionCard';
import { ToolApprovalCard } from './ToolApprovalCard';
import {
  Loader2,
  AlertCircle,
  Paperclip,
  X,
  ArrowDown,
  Folder,
} from 'lucide-react';

const WELCOME_SUGGESTIONS = [
  '分析上传的 PDF 文件',
  '帮我写一个 Python 爬虫',
  '解释这一段 React 代码',
  '生成一份周报模板',
] as const;

const NEW_SESSION_DRAFT_KEY = '__new_session__';

interface MessageDraft {
  draftId: string;
  revision: number;
  input: string;
  attachedFiles: FileInfo[];
}

interface ComposerDraftState {
  messageDrafts: Record<string, MessageDraft>;
  skillDrafts: Record<string, SkillDraft>;
}

let fallbackDraftId = 0;

function createMessageDraft(): MessageDraft {
  const draftId = globalThis.crypto?.randomUUID?.()
    ?? `draft-${Date.now()}-${fallbackDraftId++}`;
  return {
    draftId,
    revision: 0,
    input: '',
    attachedFiles: [],
  };
}

function isPristineMessageDraft(draft: MessageDraft): boolean {
  return draft.revision === 0
    && draft.input.length === 0
    && draft.attachedFiles.length === 0;
}

interface ChatV2Props {
  sessionId: string;
  onTitleUpdated?: () => void;
  onExecutionStart?: (sessionId: string) => void;
  onExecutionEnd?: (sessionId?: string) => void;
  onPanelToggle?: (isOpen: boolean) => void;
  selectedModelId: string;
  onModelChange: (modelId: string) => void;
  availableModels?: ModelInfo[];
  onCreateSession?: (modelId?: string) => Promise<string>;
  onSessionCreated?: (sessionId: string) => void;
  activeSlotSessionIds?: Set<string>;
  scrollTarget?: {
    sessionId: string;
    roundId: string;
    nonce: number;
  } | null;
}

interface DisplayRoundGroup {
  round: RoundData;
  sourceRoundIds: string[];
}

function mergeApprovalContinuation(base: RoundData, continuation: RoundData): RoundData {
  return {
    ...base,
    last_event_sequence: continuation.last_event_sequence ?? base.last_event_sequence,
    final_response: continuation.final_response || base.final_response,
    steps: [...base.steps, ...continuation.steps],
    step_count: base.step_count + continuation.step_count,
    status: continuation.status,
    completed_at: continuation.completed_at,
    interrupt: continuation.interrupt,
  };
}

function buildDisplayRoundGroups(rounds: RoundData[]): DisplayRoundGroup[] {
  const groups: DisplayRoundGroup[] = [];

  for (const round of rounds) {
    const isApprovalContinuation = round.control_kind === 'tool_approval';
    if (isApprovalContinuation) {
      const parentGroupIndex = round.parent_run_id
        ? groups.findIndex((group) => group.sourceRoundIds.includes(round.parent_run_id!))
        : groups.length - 1;

      if (parentGroupIndex >= 0) {
        const parentGroup = groups[parentGroupIndex];
        groups[parentGroupIndex] = {
          round: mergeApprovalContinuation(parentGroup.round, round),
          sourceRoundIds: [...parentGroup.sourceRoundIds, round.round_id],
        };
        continue;
      }
    }

    groups.push({ round, sourceRoundIds: [round.round_id] });
  }

  return groups;
}

export function ChatV2(props: ChatV2Props) {
  const runtime = useChatRuntimeOptional();
  if (!runtime) {
    return (
      <ChatRuntimeProvider
        onTitleUpdated={props.onTitleUpdated}
        onExecutionStart={props.onExecutionStart}
        onExecutionEnd={props.onExecutionEnd}
      >
        <ChatV2View {...props} />
      </ChatRuntimeProvider>
    );
  }
  return <ChatV2View {...props} />;
}

function ChatV2View(props: ChatV2Props) {
  const {
    sessionId,
    selectedModelId,
    onModelChange,
    availableModels = [],
    onCreateSession,
    onSessionCreated,
    activeSlotSessionIds,
    scrollTarget,
  } = props;
  const runtime = useChatRuntime();
  const projection = runtime.getSessionProjection(sessionId);
  const rounds = projection.rounds;
  const displayRoundGroups = buildDisplayRoundGroups(rounds);
  const loading = projection.loading;
  const sending = projection.sending;
  const resuming = projection.resuming;
  const pendingInterrupt = projection.pendingInterrupt;
  const runtimeError = projection.error;

  const [disableInitialMotion, setDisableInitialMotion] = useState(false);
  const [highlightedRoundId, setHighlightedRoundId] = useState<string | null>(null);
  const initialDraftKey = sessionId || NEW_SESSION_DRAFT_KEY;
  const [composerDrafts, setComposerDrafts] = useState<ComposerDraftState>(() => ({
    messageDrafts: { [initialDraftKey]: createMessageDraft() },
    skillDrafts: {},
  }));
  const [localError, setLocalError] = useState('');
  const [creatingDraftId, setCreatingDraftId] = useState<string | null>(null);
  const [isFilesOpen, setIsFilesOpen] = useState(false);
  const [filePanelTarget, setFilePanelTarget] = useState<{ file: FileInfo; nonce: number } | null>(null);
  const [assistantFileMatches, setAssistantFileMatches] = useState<Record<string, FileInfo>>({});
  const [previewFile, setPreviewFile] = useState<FileInfo | null>(null);
  const [previewSessionId, setPreviewSessionId] = useState<string>('');
  const [uploadingDraftIds, setUploadingDraftIds] = useState<Set<string>>(new Set());
  const [isDragging, setIsDragging] = useState(false);
  const [isAtBottom, setIsAtBottom] = useState(true);
  const [showScrollButton, setShowScrollButton] = useState(false);
  const [stopping, setStopping] = useState(false);
  const [dismissedInterruptId, setDismissedInterruptId] = useState<string | null>(null);

  const assistantFileMatchesRef = useRef<Record<string, FileInfo>>({});
  const assistantFileMissRoundCountsRef = useRef<Record<string, number>>({});
  const pendingAssistantFileRoundCountsRef = useRef<Record<string, number>>({});
  const messagesEndRef = useRef<HTMLDivElement>(null);
  const chatAreaRef = useRef<HTMLDivElement>(null);
  const prevRoundsLengthRef = useRef<number>(0);
  const isInitialLoadRef = useRef<boolean>(true);
  const sessionIdRef = useRef(sessionId);
  const composerDraftsRef = useRef(composerDrafts);
  const uploadsInFlightRef = useRef(new Set<string>());
  const roundElementRefs = useRef<Record<string, HTMLDivElement | null>>({});
  const handledScrollTargetNonceRef = useRef<number | null>(null);
  const suppressAutoScrollRef = useRef<boolean>(false);
  const pendingSendSessionKeysRef = useRef<Set<string>>(new Set());
  const selectedModel = availableModels.find((m) => m.id === selectedModelId);
  const currentDraftKey = sessionId || NEW_SESSION_DRAFT_KEY;
  const currentMessageDraft = composerDrafts.messageDrafts[currentDraftKey] || {
    draftId: `uninitialized:${currentDraftKey}`,
    revision: 0,
    input: '',
    attachedFiles: [],
  };
  const currentSkillDraft = composerDrafts.skillDrafts[currentDraftKey] || { keys: [], revision: 0 };
  const input = currentMessageDraft.input;
  const attachedFiles = currentMessageDraft.attachedFiles;
  const currentDraftId = currentMessageDraft.draftId;
  const creatingCurrentDraft = creatingDraftId === currentDraftId;
  const uploadingCurrentDraft = uploadingDraftIds.has(currentDraftId);
  const displayError = localError || runtimeError;
  const hasActiveSlot = activeSlotSessionIds?.has(sessionId) ?? false;

  sessionIdRef.current = sessionId;
  composerDraftsRef.current = composerDrafts;

  useEffect(() => {
    setComposerDrafts((previous) => {
      if (previous.messageDrafts[currentDraftKey]) return previous;
      return {
        ...previous,
        messageDrafts: {
          ...previous.messageDrafts,
          [currentDraftKey]: createMessageDraft(),
        },
      };
    });
  }, [currentDraftKey]);

  const addUploadingDraftId = (draftId: string) => {
    setUploadingDraftIds((previous) => {
      if (previous.has(draftId)) return previous;
      const next = new Set(previous);
      next.add(draftId);
      return next;
    });
  };

  const removeUploadingDraftId = (draftId: string) => {
    setUploadingDraftIds((previous) => {
      if (!previous.has(draftId)) return previous;
      const next = new Set(previous);
      next.delete(draftId);
      return next;
    });
  };

  const clearCreatingDraftId = (draftId: string) => {
    setCreatingDraftId((previous) => (previous === draftId ? null : previous));
  };

  const updateMessageDraft = (
    draftKey: string,
    updater: (draft: MessageDraft) => MessageDraft,
    expectedDraftId?: string,
  ) => {
    setComposerDrafts((previous) => {
      const current = previous.messageDrafts[draftKey] || createMessageDraft();
      if (expectedDraftId && current.draftId !== expectedDraftId) return previous;
      const next = updater(current);
      if (next === current) return previous;
      return {
        ...previous,
        messageDrafts: {
          ...previous.messageDrafts,
          [draftKey]: next,
        },
      };
    });
  };

  const migrateDraftsToSession = (
    sourceKey: string,
    targetSessionId: string,
    expectedDraftId: string,
  ) => {
    if (sourceKey === targetSessionId) return;
    setComposerDrafts((previous) => {
      const sourceMessage = previous.messageDrafts[sourceKey];
      if (!sourceMessage || sourceMessage.draftId !== expectedDraftId) return previous;

      const targetMessage = previous.messageDrafts[targetSessionId];
      const targetSkill = previous.skillDrafts[targetSessionId];
      const targetMessageCompatible = !targetMessage
        || targetMessage.draftId === expectedDraftId
        || isPristineMessageDraft(targetMessage);
      const targetSkillCompatible = !targetSkill
        || (targetSkill.revision === 0 && targetSkill.keys.length === 0);
      if (!targetMessageCompatible || !targetSkillCompatible) return previous;

      const nextMessageDrafts = { ...previous.messageDrafts };
      nextMessageDrafts[targetSessionId] = targetMessage?.draftId === expectedDraftId
        && targetMessage.revision > sourceMessage.revision
        ? targetMessage
        : sourceMessage;
      delete nextMessageDrafts[sourceKey];

      const nextSkillDrafts = { ...previous.skillDrafts };
      const sourceSkill = nextSkillDrafts[sourceKey];
      if (sourceSkill) nextSkillDrafts[targetSessionId] = sourceSkill;
      delete nextSkillDrafts[sourceKey];

      return {
        messageDrafts: nextMessageDrafts,
        skillDrafts: nextSkillDrafts,
      };
    });
  };

  useEffect(() => {
    if (!sessionId) {
      setIsFilesOpen(false);
      setFilePanelTarget(null);
    }
  }, [sessionId]);

  useEffect(() => {
    const visibleRunKey = projection.visibleAgentStateRunKey;
    const visibleState = visibleRunKey ? projection.agentStateByRunKey[visibleRunKey] : undefined;
    if (import.meta.env.DEV && visibleState) {
      (window as any).__agentState = visibleState;
    }
  }, [projection.agentStateByRunKey, projection.visibleAgentStateRunKey]);

  const resetAssistantFileMatches = () => {
    assistantFileMatchesRef.current = {};
    assistantFileMissRoundCountsRef.current = {};
    pendingAssistantFileRoundCountsRef.current = {};
    setAssistantFileMatches({});
  };

  const buildDisplayMessage = (text: string, files: FileInfo[]) => {
    const trimmed = text.trim();
    if (trimmed) return trimmed;
    return files.length > 0 ? '' : '[空消息]';
  };

  const buildContentBlocks = (text: string, files: FileInfo[]): ChatContentBlock[] => {
    const blocks: ChatContentBlock[] = [];
    const trimmed = text.trim();
    const toMimeType = (value?: string) => (value && value.includes('/') ? value : undefined);
    if (trimmed) {
      const textLength = [...trimmed].length;
      if (textLength > MAX_TEXT_BLOCK_CHARS) {
        throw new Error(messageTooLongText(textLength));
      }
      blocks.push({ type: 'text', text: trimmed });
    }

    const imageFiles = files.filter((file) => isImageFile(file));
    if (imageFiles.length > 0 && !(selectedModel?.supports_image ?? false)) {
      throw new Error(`当前模型 ${selectedModel?.name || selectedModelId} 不支持图片输入`);
    }
    if (imageFiles.length > 0 && imageFiles.length > (selectedModel?.max_images ?? 0)) {
      throw new Error(`当前模型最多支持 ${selectedModel?.max_images ?? 0} 张图片`);
    }

    for (const file of files) {
      const isImage = isImageFile(file);
      if (isImage && file.data_url && (selectedModel?.supports_image ?? false)) {
        blocks.push({
          type: 'image_url',
          image_url: { url: file.data_url },
          file: {
            path: file.path,
            name: file.name,
            mime_type: toMimeType(file.type),
            size: file.size,
          },
        });
      } else {
        blocks.push({
          type: 'file',
          file: {
            path: file.path,
            name: file.name,
            mime_type: toMimeType(file.type),
            size: file.size,
          },
        });
      }
    }

    if (blocks.length === 0) {
      throw new Error('消息内容不能为空');
    }
    return blocks;
  };

  useEffect(() => {
    if (!sessionId) {
      setLocalError('');
      setPreviewFile(null);
      setPreviewSessionId('');
      setIsFilesOpen(false);
      setFilePanelTarget(null);
      resetAssistantFileMatches();
      setIsDragging(false);
      setDisableInitialMotion(false);
      setHighlightedRoundId(null);
      setDismissedInterruptId(null);
      roundElementRefs.current = {};
      isInitialLoadRef.current = true;
      suppressAutoScrollRef.current = false;
      return;
    }

    setDisableInitialMotion(true);
    isInitialLoadRef.current = true;
    suppressAutoScrollRef.current = true;
    roundElementRefs.current = {};
    setHighlightedRoundId(null);
    setDismissedInterruptId(null);
    setFilePanelTarget(null);
    resetAssistantFileMatches();
    setIsAtBottom(false);
    prevRoundsLengthRef.current = 0;
    setLocalError('');
    setStopping(false);
    void runtime.loadSessionHistory(sessionId, { hasActiveSlot });
  }, [sessionId]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if (!sessionId || sending || resuming) return;

    const pendingRoundCounts = pendingAssistantFileRoundCountsRef.current;
    const verificationRoundCount = rounds.length;
    const candidates = collectAssistantFileCandidates(rounds, sessionId).filter((file) => {
      const cachedMatch = assistantFileMatchesRef.current[file.path];
      if (cachedMatch) return false;

      const pendingRoundCount = pendingRoundCounts[file.path];
      if (pendingRoundCount !== undefined && pendingRoundCount >= verificationRoundCount) return false;

      const lastMissRoundCount = assistantFileMissRoundCountsRef.current[file.path];
      return lastMissRoundCount === undefined || lastMissRoundCount < verificationRoundCount;
    });
    if (candidates.length === 0) return;

    const groupedByParent = new Map<string, FileInfo[]>();
    for (const file of candidates) {
      pendingRoundCounts[file.path] = verificationRoundCount;
      const targetPath = normalizeAssistantTargetPath(file.path);
      const parentPath = getAssistantTargetParentPath(targetPath);
      const group = groupedByParent.get(parentPath) || [];
      group.push(file);
      groupedByParent.set(parentPath, group);
    }

    const targetSessionId = sessionId;
    void Promise.all(Array.from(groupedByParent.entries()).map(async ([parentPath, files]) => {
      let updates: Record<string, FileInfo | null>;
      try {
        const list = await apiService.getSessionFiles(targetSessionId, parentPath || undefined);
        updates = buildAssistantFileMatchUpdates(files, list.files, targetSessionId);
      } catch (err) {
        console.warn('Failed to verify assistant file references:', err);
        updates = {};
      }

      if (sessionIdRef.current !== targetSessionId) {
        for (const file of files) {
          if (pendingRoundCounts[file.path] === verificationRoundCount) delete pendingRoundCounts[file.path];
        }
        return;
      }

      const currentPaths = new Set(
        files
          .filter((file) => pendingRoundCounts[file.path] === verificationRoundCount)
          .map((file) => file.path),
      );
      for (const file of files) {
        if (pendingRoundCounts[file.path] === verificationRoundCount) delete pendingRoundCounts[file.path];
      }

      const currentUpdates = Object.fromEntries(
        Object.entries(updates).filter(([path]) => currentPaths.has(path)),
      );
      if (Object.keys(currentUpdates).length === 0) return;

      const nextMissRoundCounts = { ...assistantFileMissRoundCountsRef.current };
      for (const [path, match] of Object.entries(currentUpdates)) {
        if (match) delete nextMissRoundCounts[path];
        else nextMissRoundCounts[path] = verificationRoundCount;
      }
      const matchedUpdates = Object.fromEntries(
        Object.entries(currentUpdates).filter(([, match]) => !!match),
      ) as Record<string, FileInfo>;
      assistantFileMissRoundCountsRef.current = nextMissRoundCounts;
      if (Object.keys(matchedUpdates).length === 0) return;
      assistantFileMatchesRef.current = {
        ...assistantFileMatchesRef.current,
        ...matchedUpdates,
      };
      setAssistantFileMatches((prev) => ({ ...prev, ...matchedUpdates }));
    }));
  }, [rounds, sessionId, sending, resuming]);

  useLayoutEffect(() => {
    const container = chatAreaRef.current;
    if (!isInitialLoadRef.current || !container || loading) return;
    isInitialLoadRef.current = false;

    const hasExplicitScrollTarget = scrollTarget?.sessionId === sessionId && Boolean(scrollTarget.roundId);
    if (!hasExplicitScrollTarget) {
      messagesEndRef.current?.scrollIntoView({ behavior: 'auto' });
    }

    const distanceFromBottom = container.scrollHeight - container.scrollTop - container.clientHeight;
    const atBottom = !hasExplicitScrollTarget || distanceFromBottom < 100;
    setIsAtBottom(atBottom);
    setShowScrollButton(!atBottom);
    suppressAutoScrollRef.current = false;
    prevRoundsLengthRef.current = rounds.reduce((sum, round) => sum + 1 + round.steps.length, 0);
  }, [loading, rounds, sessionId, scrollTarget?.roundId, scrollTarget?.sessionId]);

  useEffect(() => {
    if (!scrollTarget || scrollTarget.sessionId !== sessionId || loading) return;
    if (handledScrollTargetNonceRef.current === scrollTarget.nonce) return;
    const target = roundElementRefs.current[scrollTarget.roundId];
    if (!target) return;
    target.scrollIntoView({ behavior: 'smooth', block: 'center' });
    handledScrollTargetNonceRef.current = scrollTarget.nonce;
    setHighlightedRoundId(scrollTarget.roundId);
    const timer = setTimeout(() => {
      setHighlightedRoundId((current) => (current === scrollTarget.roundId ? null : current));
    }, 1800);
    return () => clearTimeout(timer);
  }, [scrollTarget?.nonce, scrollTarget?.roundId, scrollTarget?.sessionId, sessionId, loading, rounds]);

  useEffect(() => {
    if (suppressAutoScrollRef.current) return;
    const currentLength = rounds.reduce((sum, round) => sum + 1 + round.steps.length, 0);
    const hasNewContent = currentLength > prevRoundsLengthRef.current;
    if (isAtBottom) {
      messagesEndRef.current?.scrollIntoView({ behavior: hasNewContent ? 'smooth' : 'auto' });
    }
    prevRoundsLengthRef.current = currentLength;
  }, [rounds, isAtBottom]);

  useEffect(() => {
    const container = chatAreaRef.current;
    if (!container) return;
    const handleScroll = () => {
      const { scrollTop, scrollHeight, clientHeight } = container;
      const distanceFromBottom = scrollHeight - scrollTop - clientHeight;
      const atBottom = distanceFromBottom < 100;
      setIsAtBottom(atBottom);
      setShowScrollButton(!atBottom);
    };
    container.addEventListener('scroll', handleScroll);
    return () => container.removeEventListener('scroll', handleScroll);
  }, [sessionId]);

  const scrollToBottom = (force: boolean = false) => {
    if (force) setIsAtBottom(true);
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  };

  const handleFileUpload = async (files: FileList | File[] | null) => {
    const capturedDraftId = currentMessageDraft.draftId;
    if (
      !files
      || files.length === 0
      || uploadsInFlightRef.current.has(capturedDraftId)
      || sending
      || resuming
      || creatingDraftId === capturedDraftId
      || stopping
      || pendingSendSessionKeysRef.current.has(currentDraftKey)
    ) return;
    const sourceDraftKey = currentDraftKey;
    uploadsInFlightRef.current.add(capturedDraftId);
    addUploadingDraftId(capturedDraftId);
    const uploadedFiles: FileInfo[] = [];
    try {
      let targetSessionId = sessionId;
      if (!targetSessionId) {
        if (!onCreateSession) {
          setLocalError('无法创建会话');
          removeUploadingDraftId(capturedDraftId);
          return;
        }
        setCreatingDraftId(capturedDraftId);
        try {
          targetSessionId = await onCreateSession(selectedModelId || undefined);
        } catch (err) {
          console.error('Failed to create session for file upload:', err);
          setLocalError('创建会话失败，无法上传文件');
          clearCreatingDraftId(capturedDraftId);
          removeUploadingDraftId(capturedDraftId);
          return;
        }
      }

      const sourceDraft = composerDraftsRef.current.messageDrafts[sourceDraftKey];
      if (!sourceDraft || sourceDraft.draftId !== capturedDraftId) return;
      migrateDraftsToSession(sourceDraftKey, targetSessionId, capturedDraftId);
      if (!sessionIdRef.current) onSessionCreated?.(targetSessionId);

      const uploadQueue = Array.from(files as ArrayLike<File>);
      for (const file of uploadQueue) {
        const fileInfo = await apiService.uploadFile(targetSessionId, file);
        fileInfo.session_id = targetSessionId;
        if (file.type) fileInfo.type = file.type;
        if (isImageFile(fileInfo)) fileInfo.data_url = await readFileAsDataUrl(file);
        uploadedFiles.push(fileInfo);
      }
      updateMessageDraft(
        targetSessionId,
        (draft) => ({
          ...draft,
          revision: draft.revision + 1,
          attachedFiles: [...draft.attachedFiles, ...uploadedFiles],
        }),
        capturedDraftId,
      );
    } catch (err) {
      console.error('Failed to upload files:', err);
      setLocalError(formatUploadError(err));
    } finally {
      uploadsInFlightRef.current.delete(capturedDraftId);
      removeUploadingDraftId(capturedDraftId);
      clearCreatingDraftId(capturedDraftId);
    }
  };

  const handleDragOver = (event: React.DragEvent) => {
    event.preventDefault();
    event.stopPropagation();
    if (event.dataTransfer.types.includes('Files')) setIsDragging(true);
  };

  const handleDragLeave = (event: React.DragEvent) => {
    event.preventDefault();
    event.stopPropagation();
    const rect = (event.currentTarget as HTMLElement).getBoundingClientRect();
    if (
      event.clientX <= rect.left
      || event.clientX >= rect.right
      || event.clientY <= rect.top
      || event.clientY >= rect.bottom
    ) {
      setIsDragging(false);
    }
  };

  const handleDrop = (event: React.DragEvent) => {
    event.preventDefault();
    event.stopPropagation();
    setIsDragging(false);
    if (event.dataTransfer.files.length > 0) handleFileUpload(event.dataTransfer.files);
  };

  const handleRemoveAttachment = (index: number) => {
    updateMessageDraft(currentDraftKey, (draft) => ({
      ...draft,
      revision: draft.revision + 1,
      attachedFiles: draft.attachedFiles.filter((_, itemIndex) => itemIndex !== index),
    }));
  };

  const handlePreviewAttachment = async (file: AttachmentInfo | FileInfo) => {
    const targetSessionId = file.session_id || sessionId;
    if (!targetSessionId) {
      if (isImageFile(file) && file.data_url) setPreviewFile(toFileInfo(file, ''));
      return;
    }
    let normalizedFile = toFileInfo(file, targetSessionId);
    if (normalizedFile.size <= 0) {
      try {
        const list = await apiService.getSessionFiles(targetSessionId);
        const matched = list.files.find((item) => item.path === normalizedFile.path);
        if (matched) {
          normalizedFile = {
            ...normalizedFile,
            size: matched.size,
            modified: matched.modified || normalizedFile.modified,
            type: matched.type || normalizedFile.type,
          };
        }
      } catch (err) {
        console.warn('Failed to hydrate file metadata for preview:', err);
      }
    }
    setPreviewSessionId(targetSessionId);
    setPreviewFile(normalizedFile);
  };

  const handleOpenAssistantFile = async (file: FileInfo) => {
    if (!sessionId) return;
    setLocalError('');
    const normalizedFile = toFileInfo(file, sessionId);
    const targetPath = normalizeAssistantTargetPath(normalizedFile.path);
    const parentPath = getAssistantTargetParentPath(targetPath);
    try {
      const list = await apiService.getSessionFiles(sessionId, parentPath || undefined);
      const matchedFile = list.files.find((item) => (
        !item.is_directory && normalizeAssistantTargetPath(item.path) === targetPath
      ));
      if (!matchedFile) {
        setLocalError(`文件不存在或尚未生成：${normalizedFile.name}`);
        return;
      }
      setFilePanelTarget((prev) => ({
        file: {
          ...matchedFile,
          path: normalizeAssistantTargetPath(matchedFile.path),
          session_id: sessionId,
        },
        nonce: (prev?.nonce ?? 0) + 1,
      }));
      setIsFilesOpen(true);
    } catch (err) {
      console.warn('Failed to verify assistant file target:', err);
      setLocalError(`无法确认文件是否存在：${normalizedFile.name}`);
    }
  };

  const handleInputChange = (event: React.ChangeEvent<HTMLTextAreaElement>) => {
    const nextInput = event.target.value;
    updateMessageDraft(currentDraftKey, (draft) => (
      draft.input === nextInput
        ? draft
        : { ...draft, input: nextInput, revision: draft.revision + 1 }
    ));
  };

  const handleSelectFile = (file: FileInfo, _newInput: string) => {
    updateMessageDraft(currentDraftKey, (draft) => {
      if (draft.attachedFiles.some((item) => item.name === file.name)) return draft;
      const normalizedFile = file.session_id
        ? file
        : { ...file, session_id: sessionId || undefined };
      return {
        ...draft,
        revision: draft.revision + 1,
        attachedFiles: [...draft.attachedFiles, normalizedFile],
      };
    });
  };

  const handleSelectedSkillKeysChange = (keys: string[]) => {
    const draftKey = currentDraftKey;
    setComposerDrafts((previous) => {
      const current = previous.skillDrafts[draftKey] || { keys: [], revision: 0 };
      return {
        ...previous,
        skillDrafts: {
          ...previous.skillDrafts,
          [draftKey]: { keys, revision: current.revision + 1 },
        },
      };
    });
  };

  const handleSend = async () => {
    const initialSessionKey = currentDraftKey;
    const capturedDraftId = currentMessageDraft.draftId;
    if (
      (!input.trim() && attachedFiles.length === 0)
      || sending
      || creatingDraftId === capturedDraftId
      || stopping
      || uploadingDraftIds.has(capturedDraftId)
      || uploadsInFlightRef.current.has(capturedDraftId)
      || pendingSendSessionKeysRef.current.has(initialSessionKey)
    ) return;
    const messageSnapshot: MessageDraft = {
      ...currentMessageDraft,
      attachedFiles: [...currentMessageDraft.attachedFiles],
    };
    const draftInput = messageSnapshot.input;
    const draftAttachments = messageSnapshot.attachedFiles;
    const skillSnapshot: SkillDraft = {
      keys: [...currentSkillDraft.keys],
      revision: currentSkillDraft.revision,
    };
    const clearedMessageRevision = messageSnapshot.revision + 1;
    const clearedSkillRevision = skillSnapshot.revision + 1;
    let restoreDraftKey = initialSessionKey;
    const restoreSubmissionSnapshot = () => {
      setComposerDrafts((previous) => {
        const current = previous.messageDrafts[restoreDraftKey];
        if (!current || current.draftId !== messageSnapshot.draftId) return previous;
        const nextMessageDrafts = { ...previous.messageDrafts };
        if (
          current.revision === clearedMessageRevision
        ) {
          nextMessageDrafts[restoreDraftKey] = {
            ...messageSnapshot,
            revision: current.revision + 1,
            attachedFiles: [...messageSnapshot.attachedFiles],
          };
        }
        return {
          messageDrafts: nextMessageDrafts,
          skillDrafts: restoreFailedSkillDraft(
            previous.skillDrafts,
            restoreDraftKey,
            skillSnapshot,
            clearedSkillRevision,
          ),
        };
      });
    };
    let contentBlocks: ChatContentBlock[] = [];
    try {
      contentBlocks = buildContentBlocks(draftInput, draftAttachments);
    } catch (err: any) {
      setLocalError(err?.message || '消息构建失败');
      return;
    }
    pendingSendSessionKeysRef.current.add(initialSessionKey);
    let targetSessionKey = initialSessionKey;
    try {
      const userMessage = buildDisplayMessage(draftInput, draftAttachments);
      const isStartingNewSession = !sessionId;
      setComposerDrafts((previous) => {
        const current = previous.messageDrafts[initialSessionKey];
        if (!current || current.draftId !== messageSnapshot.draftId) return previous;
        return {
          messageDrafts: {
            ...previous.messageDrafts,
            [initialSessionKey]: {
              ...current,
              revision: clearedMessageRevision,
              input: '',
              attachedFiles: [],
            },
          },
          skillDrafts: {
            ...previous.skillDrafts,
            [initialSessionKey]: { keys: [], revision: clearedSkillRevision },
          },
        };
      });
      setDisableInitialMotion(false);
      setLocalError('');

      let targetSessionId = sessionId;
      if (!targetSessionId) {
        if (!onCreateSession) {
          restoreSubmissionSnapshot();
          return;
        }
        setCreatingDraftId(capturedDraftId);
        try {
          targetSessionId = await onCreateSession(selectedModelId || undefined);
        } catch (err) {
          console.error('Failed to create session:', err);
          setLocalError('创建会话失败，请重试');
          restoreSubmissionSnapshot();
          clearCreatingDraftId(capturedDraftId);
          return;
        }
      }

      targetSessionKey = targetSessionId;
      if (targetSessionKey !== initialSessionKey) {
        const sourceDraft = composerDraftsRef.current.messageDrafts[initialSessionKey];
        if (!sourceDraft || sourceDraft.draftId !== messageSnapshot.draftId) {
          restoreSubmissionSnapshot();
          if (isStartingNewSession) clearCreatingDraftId(capturedDraftId);
          return;
        }
        restoreDraftKey = targetSessionKey;
        migrateDraftsToSession(initialSessionKey, targetSessionKey, messageSnapshot.draftId);
        if (!sessionIdRef.current) onSessionCreated?.(targetSessionKey);
      }
      pendingSendSessionKeysRef.current.add(targetSessionKey);
      if (isStartingNewSession) clearCreatingDraftId(capturedDraftId);
      const sendPromise = runtime.sendMessage({
        sessionId: targetSessionId,
        displayMessage: userMessage,
        content: contentBlocks,
        attachments: draftAttachments,
        preferredSkillKeys: skillSnapshot.keys,
        onRejectedBeforeAccept: restoreSubmissionSnapshot,
      });
      await sendPromise;
    } finally {
      pendingSendSessionKeysRef.current.delete(initialSessionKey);
      pendingSendSessionKeysRef.current.delete(targetSessionKey);
    }
  };

  const handleStop = async () => {
    if (!sessionId || !(sending || resuming) || stopping) return;
    setStopping(true);
    try {
      await runtime.stopSessionRun(sessionId);
    } finally {
      setStopping(false);
    }
  };

  const handleResumeSubmit = async (answers: Record<string, string>) => {
    if (!sessionId || !pendingInterrupt?.id) return;
    await runtime.resumeRun(sessionId, pendingInterrupt, answers);
  };

  const inputDisabled = sending || creatingCurrentDraft || resuming;
  const sendingLabel = creatingCurrentDraft ? '创建中' : resuming ? 'Resuming' : sending ? 'Running' : '';
  const hasLiveReplyBelow = showScrollButton && (sending || resuming);

  return (
    <div className="flex-1 flex h-screen bg-claude-bg relative">
      <div
        className="flex-1 flex flex-col relative"
        onDragOver={handleDragOver}
        onDragLeave={handleDragLeave}
        onDrop={handleDrop}
      >
        {isDragging && (
          <div className="absolute inset-0 flex items-center justify-center bg-claude-accent/5 backdrop-blur-sm z-50 pointer-events-none border-2 border-dashed border-claude-accent/40 rounded-2xl m-4">
            <div className="text-center">
              <Paperclip className="w-8 h-8 text-claude-accent mx-auto mb-3" />
              <p className="text-claude-text font-medium mb-1">释放以上传文件</p>
              <p className="text-claude-muted text-sm">{sessionId ? '支持拖放到对话框任意位置' : '支持拖放到输入框或页面'}</p>
            </div>
          </div>
        )}

        <header className="h-14 flex items-center justify-between px-6 bg-claude-bg/80 backdrop-blur-sm border-b border-claude-border sticky top-0 z-20">
          <ModelSelector
            selectedModelId={selectedModelId}
            onModelChange={onModelChange}
            availableModels={availableModels}
            readOnly={!!sessionId}
          />

          {sessionId ? (
            <button
              type="button"
              onClick={() => {
                setFilePanelTarget(null);
                setIsFilesOpen(!isFilesOpen);
              }}
              className={`h-9 px-3 rounded-xl border transition-[background-color,color,border-color,transform] active:scale-95 flex items-center gap-2 ${
                isFilesOpen
                  ? 'border-[#2f6f54] bg-[#eef8f2] text-[#234d3c]'
                  : 'border-transparent text-claude-secondary hover:bg-claude-hover'
              }`}
              title="会话资源"
            >
              <Folder size={16} />
              <span className="text-sm hidden sm:inline">Files</span>
            </button>
          ) : (
            <div className="w-[88px]" />
          )}
        </header>

        <div ref={chatAreaRef} className={`flex-1 relative bg-claude-bg ${isFilesOpen ? 'overflow-hidden' : 'overflow-y-auto'}`}>
          {sessionId && !isFilesOpen && (
            <ArtifactsPanel
              sessionId={sessionId}
              isOpen={false}
              onClose={() => {
                setIsFilesOpen(false);
                setFilePanelTarget(null);
              }}
              targetFile={filePanelTarget?.file || null}
              targetFileNonce={filePanelTarget?.nonce}
              variant="workspace"
            />
          )}
          {isFilesOpen && sessionId ? (
            <ArtifactsPanel
              sessionId={sessionId}
              isOpen={isFilesOpen}
              onClose={() => {
                setIsFilesOpen(false);
                setFilePanelTarget(null);
              }}
              targetFile={filePanelTarget?.file || null}
              targetFileNonce={filePanelTarget?.nonce}
              variant="workspace"
            />
          ) : loading ? (
            <div className="flex items-center justify-center h-full">
              <div className="text-center">
                <Loader2 className="w-6 h-6 text-claude-muted animate-spin mx-auto mb-3" />
                <p className="text-claude-muted text-sm">正在同步会话...</p>
              </div>
            </div>
          ) : rounds.length === 0 ? (
            <div className="flex items-center justify-center h-full">
              <div className="text-center max-w-lg px-6">
                <h2 className="text-3xl font-medium text-claude-text mb-3">你好，有什么可以帮你的？</h2>
                <p className="text-claude-secondary leading-relaxed mb-10">
                  编写代码、分析数据、处理文件，或者解答技术问题。
                </p>
                <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                  {WELCOME_SUGGESTIONS.map((suggestion, index) => (
                    <button
                      type="button"
                      key={index}
                      onClick={() => updateMessageDraft(currentDraftKey, (draft) => ({
                        ...draft,
                        input: suggestion,
                        revision: draft.revision + 1,
                      }))}
                      className="px-4 py-3 bg-white border border-claude-border rounded-2xl text-sm text-claude-secondary hover:border-claude-border-strong hover:bg-claude-hover transition-colors text-left"
                    >
                      {suggestion}
                    </button>
                  ))}
                </div>
              </div>
            </div>
          ) : (
            <div className="mx-auto px-4 md:px-8 py-6 space-y-6 max-w-3xl">
              {displayRoundGroups.map(({ round, sourceRoundIds }, index) => (
                <div
                  key={round.round_id}
                  ref={(el) => {
                    sourceRoundIds.forEach((roundId) => {
                      roundElementRefs.current[roundId] = el;
                    });
                  }}
                  data-round-id={round.round_id}
                  className={`scroll-mt-20 rounded-2xl transition-colors duration-300 ${
                    highlightedRoundId === round.round_id
                      ? 'bg-claude-accent/10 ring-2 ring-claude-accent/30 px-3 py-3 -mx-3'
                      : ''
                  }`}
                >
                  <Round
                    round={round}
                    userAttachments={round.user_attachments || []}
                    sessionId={sessionId}
                    assistantFileMatches={assistantFileMatches}
                    onPreviewAttachment={handlePreviewAttachment}
                    onOpenFileInPanel={handleOpenAssistantFile}
                    isStreaming={(sending || resuming) && index === displayRoundGroups.length - 1}
                    disableMotion={disableInitialMotion}
                  />
                </div>
              ))}
              <div ref={messagesEndRef} />
            </div>
          )}

          {!isFilesOpen && showScrollButton && (
            <button
              type="button"
              onClick={() => scrollToBottom(true)}
              className={`fixed bottom-28 right-8 z-10 flex items-center gap-2 bg-white text-claude-text shadow-lg border border-claude-border transition-[transform,box-shadow] hover:scale-105 active:scale-95 ${
                hasLiveReplyBelow ? 'live-reply-pill rounded-full px-3.5 py-2.5 ring-2 ring-claude-accent/25 shadow-xl' : 'rounded-full p-2.5'
              }`}
              aria-label={hasLiveReplyBelow ? '新回复正在生成，回到底部' : '回到底部'}
            >
              {hasLiveReplyBelow && (
                <>
                  <span className="relative flex h-2 w-2" aria-hidden="true">
                    <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-claude-accent opacity-60" />
                    <span className="relative inline-flex h-2 w-2 rounded-full bg-claude-accent" />
                  </span>
                  <span className="text-xs font-semibold whitespace-nowrap">新回复正在生成</span>
                </>
              )}
              <ArrowDown className="w-4 h-4" />
            </button>
          )}
        </div>

        {displayError && (
          <div className="px-6 py-3 bg-red-50 border-t border-red-100">
            <div className="mx-auto max-w-3xl">
              <div className="flex items-start gap-3">
                <AlertCircle className="w-4 h-4 text-claude-error flex-shrink-0 mt-0.5" />
                <div className="flex-1 min-w-0 overflow-hidden">
                  <pre className="text-xs whitespace-pre-wrap break-words font-mono text-claude-error overflow-x-auto max-w-full">
                    {displayError}
                  </pre>
                </div>
                <button
                  onClick={() => {
                    setLocalError('');
                    if (sessionId) runtime.clearError(sessionId);
                  }}
                  className="p-1 hover:bg-red-100 rounded-full transition-colors"
                  aria-label="关闭错误提示"
                >
                  <X className="w-3.5 h-3.5 text-claude-error" />
                </button>
              </div>
            </div>
          </div>
        )}

        {pendingInterrupt && pendingInterrupt.id !== dismissedInterruptId && pendingInterrupt.reason === 'input_required' && pendingInterrupt.payload?.questions && (pendingInterrupt.payload.questions as AskUserQuestion[]).length > 0 && (
          <div className="relative z-20 px-4 md:px-8 mb-[-3.5rem] mx-auto w-full max-w-3xl">
            <QuestionCard
              questions={pendingInterrupt.payload.questions as AskUserQuestion[]}
              onSubmit={handleResumeSubmit}
              onDismiss={() => {
                setDismissedInterruptId(pendingInterrupt.id || 'dismissed');
              }}
              disabled={resuming}
            />
          </div>
        )}

        {pendingInterrupt && pendingInterrupt.id !== dismissedInterruptId && pendingInterrupt.reason === 'human_approval' && pendingInterrupt.payload?.kind === 'tool_approval' && (
          <div className="relative z-20 px-4 md:px-8 mb-[-3.5rem] mx-auto w-full max-w-3xl">
            <ToolApprovalCard
              approval={pendingInterrupt.payload as ToolApprovalPayload}
              onSubmit={handleResumeSubmit}
              onDismiss={() => {
                setDismissedInterruptId(pendingInterrupt.id || 'dismissed');
              }}
              disabled={resuming}
            />
          </div>
        )}

        {!isFilesOpen && (
          <ChatInput
            value={input}
            onChange={(value) => updateMessageDraft(currentDraftKey, (draft) => (
              draft.input === value
                ? draft
                : { ...draft, input: value, revision: draft.revision + 1 }
            ))}
            onSend={handleSend}
            onStop={(sending || resuming) ? handleStop : undefined}
            disabled={inputDisabled}
            sendDisabled={stopping || uploadingCurrentDraft}
            sendingLabel={sendingLabel}
            placeholder={sessionId ? '输入指令...' : '输入你的问题，按 Enter 开始对话...'}
            autoFocus={!sessionId}
            attachedFiles={attachedFiles}
            onRemoveAttachment={handleRemoveAttachment}
            onFileUpload={handleFileUpload}
            onInputDropHandled={() => setIsDragging(false)}
            onPreviewAttachment={sessionId ? handlePreviewAttachment : undefined}
            uploading={uploadingCurrentDraft}
            onInputChangeRaw={handleInputChange}
            onFileSelected={handleSelectFile}
            selectedSkillKeys={currentSkillDraft.keys}
            onSelectedSkillKeysChange={handleSelectedSkillKeysChange}
          />
        )}
      </div>

      {previewFile && previewSessionId && (
        <FilePreview
          sessionId={previewSessionId}
          file={previewFile}
          onClose={() => {
            setPreviewFile(null);
            setPreviewSessionId('');
          }}
        />
      )}
    </div>
  );
}

function normalizeAssistantTargetPath(path: string): string {
  return path.replace(/^\/+/, '');
}

function getAssistantTargetParentPath(path: string): string {
  const normalizedPath = normalizeAssistantTargetPath(path);
  return normalizedPath.includes('/')
    ? normalizedPath.substring(0, normalizedPath.lastIndexOf('/'))
    : '';
}

function collectAssistantFileCandidates(rounds: RoundData[], sessionId: string): FileInfo[] {
  const byPath = new Map<string, FileInfo>();
  for (const round of rounds) {
    const assistantContent = getRoundAssistantContent(round);
    if (!assistantContent) continue;
    for (const file of extractAssistantFiles(assistantContent, sessionId)) {
      if (!byPath.has(file.path)) byPath.set(file.path, file);
    }
  }
  return Array.from(byPath.values());
}

function getRoundAssistantContent(round: RoundData): string {
  if (round.final_response) return round.final_response;
  return [...round.steps].reverse().find((step) => step.assistant_content)?.assistant_content || '';
}

function buildAssistantFileMatchUpdates(
  candidates: FileInfo[],
  listedFiles: FileInfo[],
  sessionId: string,
): Record<string, FileInfo | null> {
  const listedByPath = new Map(
    listedFiles
      .filter((file) => !file.is_directory)
      .map((file) => [normalizeAssistantTargetPath(file.path), file]),
  );

  return Object.fromEntries(candidates.map((candidate) => {
    const targetPath = normalizeAssistantTargetPath(candidate.path);
    const matched = listedByPath.get(targetPath);
    return [
      candidate.path,
      matched
        ? {
            ...matched,
            path: targetPath,
            session_id: sessionId,
          }
        : null,
    ];
  }));
}
