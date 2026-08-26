import { useEffect, useLayoutEffect, useMemo, useRef, useState, type CSSProperties, type Ref } from 'react';

import { apiService } from '../services/api';
import {
  AttachmentInfo,
  AskUserQuestion,
  ChatContentBlock,
  FileInfo,
  ModelInfo,
  PreferredMcpConnectionSnapshot,
  RoundData,
  ToolApprovalPayload,
  TurnReasoningSelection,
} from '../types';
import {
  ChatRuntimeProvider,
  useChatRuntime,
  useChatRuntimeOptional,
} from '../runtime/ChatRuntimeProvider';
import { readFileAsDataUrl } from '../utils/imageUtils';
import { toFileInfo, isImageFile } from '../utils/fileUtils';
import { extractAssistantFiles } from '../utils/assistantFileRefs';
import {
  emptyTurnPreferenceDraft,
  restoreFailedTurnPreferenceDraft,
  type TurnPreferenceDraft,
} from '../utils/turnPreferenceDrafts';
import {
  MAX_TEXT_BLOCK_CHARS,
  formatUploadError,
  messageTooLongText,
} from '../utils/errorMessages';
import { Round } from './Round';
import { ArtifactsPanel, type ArtifactsPanelHandle } from './ArtifactsPanel';
import { FilePreview, type SessionFileOwnerIdentity } from './FilePreview';
import { ModelSelector } from './ModelSelector';
import { ChatInput } from './ChatInput';
import { QuestionCard } from './QuestionCard';
import { ToolApprovalCard } from './ToolApprovalCard';
import {
  ChatPaneButton,
  SessionFilesButton,
} from './session-files/SessionFilesControls';
import { SessionFilesSplitter } from './session-files/SessionFilesSplitter';
import './session-files/session-files.css';
import {
  Loader2,
  AlertCircle,
  Paperclip,
  X,
  ArrowDown,
} from 'lucide-react';

const WELCOME_SUGGESTIONS = [
  '分析上传的 PDF 文件',
  '帮我写一个 Python 爬虫',
  '解释这一段 React 代码',
  '生成一份周报模板',
] as const;

const NEW_SESSION_DRAFT_KEY = '__new_session__';
const SESSION_FILES_RATIO_STORAGE_KEY = 'opencapybox.sessionFiles.chatRatio';
const MOBILE_FILES_QUERY = '(max-width: 1199px)';
const DEFAULT_SESSION_FILES_CHAT_RATIO = 45;

type SessionFilesLayout = 'closed' | 'split' | 'full';

interface SessionFilesViewState {
  layout: SessionFilesLayout;
  chatRatio: number;
}

function normalizeVisibleChatRatio(value: number): number {
  return Number.isFinite(value) && value > 0 && value < 100
    ? value
    : DEFAULT_SESSION_FILES_CHAT_RATIO;
}

function createSessionFilesViewState(chatRatio: number): SessionFilesViewState {
  return {
    layout: 'closed',
    chatRatio: normalizeVisibleChatRatio(chatRatio),
  };
}

function readInitialChatRatio(): number {
  try {
    const storedValue = window.localStorage.getItem(SESSION_FILES_RATIO_STORAGE_KEY);
    if (storedValue === null) return DEFAULT_SESSION_FILES_CHAT_RATIO;
    const stored = Number(storedValue);
    if (Number.isFinite(stored) && stored > 0 && stored < 100) return stored;
  } catch {
    // Storage may be unavailable in privacy-restricted contexts.
  }
  return DEFAULT_SESSION_FILES_CHAT_RATIO;
}

function readIsMobileFilesViewport(): boolean {
  return typeof window.matchMedia === 'function'
    ? window.matchMedia(MOBILE_FILES_QUERY).matches
    : false;
}

function defaultTurnReasoning(model?: ModelInfo): TurnReasoningSelection | null {
  const supportsControl = model?.supports_reasoning_control
    ?? Boolean(model?.supported_reasoning_efforts?.length);
  if (!model || model.provider !== 'openai' || !supportsControl) return null;
  // Current catalogs expose the authoritative transport pair directly. The
  // human-readable default_reasoning_level is a lossy projection (for example,
  // both provider_default+high and enabled+high display as High), so it must
  // never override that pair.
  if (model.thinking_mode) {
    return {
      mode: model.thinking_mode,
      effort: model.thinking_mode === 'disabled' ? null : (model.reasoning_effort || null),
    };
  }

  // Compatibility for older API responses that only exposed the projection.
  if (model.default_reasoning_level === 'off') return { mode: 'disabled', effort: null };
  if (model.default_reasoning_level === 'on') return { mode: 'enabled', effort: null };
  if (model.default_reasoning_level) {
    return {
      mode: 'enabled',
      effort: model.default_reasoning_level,
    };
  }
  if (model.reasoning_effort) {
    return {
      mode: 'provider_default',
      effort: model.reasoning_effort,
    };
  }
  return { mode: 'provider_default', effort: null };
}

interface MessageDraft {
  draftId: string;
  revision: number;
  input: string;
  attachedFiles: FileInfo[];
}

interface ReasoningDraft {
  modelId: string;
  selection: TurnReasoningSelection | null;
}

interface ComposerDraftState {
  messageDrafts: Record<string, MessageDraft>;
  preferenceDrafts: Record<string, TurnPreferenceDraft>;
  reasoningDrafts: Record<string, ReasoningDraft>;
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
  selectedModelId: string;
  onModelChange: (modelId: string) => void;
  availableModels?: ModelInfo[];
  onCreateSession?: (modelId?: string) => Promise<string>;
  onSessionCreated?: (sessionId: string) => void;
  onFilesFullChange?: (full: boolean) => void;
  sessionFilesHandleRef?: Ref<ArtifactsPanelHandle>;
  onStartEdgeCollapseSidebar?: () => void;
  activeSlotSessionIds?: Set<string>;
  scrollTarget?: {
    sessionId: string;
    roundId: string;
    nonce: number;
  } | null;
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
    onFilesFullChange,
    sessionFilesHandleRef,
    onStartEdgeCollapseSidebar,
    activeSlotSessionIds,
    scrollTarget,
  } = props;
  const runtime = useChatRuntime();
  const loadSessionHistory = runtime.loadSessionHistory;
  const projection = runtime.getSessionProjection(sessionId);
  const rounds = projection.rounds;
  const loading = projection.loading;
  const sending = projection.sending;
  const resuming = projection.resuming;
  const pendingInterrupt = projection.pendingInterrupt;
  const waitingInteraction = rounds.some((round) => round.status === 'waiting_interaction');
  const runtimeError = projection.error;
  const hasLocalActiveTransport = projection.activeRunKeys.some((runKey) => {
    const source = runtime.state.runs[runKey]?.source;
    return source === 'direct' || source === 'resume';
  });

  const [disableInitialMotion, setDisableInitialMotion] = useState(false);
  const [highlightedRoundId, setHighlightedRoundId] = useState<string | null>(null);
  const initialDraftKey = sessionId || NEW_SESSION_DRAFT_KEY;
  const [composerDrafts, setComposerDrafts] = useState<ComposerDraftState>(() => ({
    messageDrafts: { [initialDraftKey]: createMessageDraft() },
    preferenceDrafts: {},
    reasoningDrafts: {},
  }));
  const [localError, setLocalError] = useState('');
  const [creatingDraftId, setCreatingDraftId] = useState<string | null>(null);
  const [defaultChatRatio] = useState(readInitialChatRatio);
  const [sessionFilesStates, setSessionFilesStates] = useState<Record<string, SessionFilesViewState>>({});
  const [isMobileFilesViewport, setIsMobileFilesViewport] = useState(readIsMobileFilesViewport);
  const [filePanelTarget, setFilePanelTarget] = useState<(
    SessionFileOwnerIdentity & { file: FileInfo; nonce: number }
  ) | null>(null);
  const [assistantFileMatches, setAssistantFileMatches] = useState<Record<string, FileInfo>>({});
  const [previewFile, setPreviewFile] = useState<FileInfo | null>(null);
  const [previewSessionId, setPreviewSessionId] = useState<string>('');
  const [uploadingDraftIds, setUploadingDraftIds] = useState<Set<string>>(new Set());
  const [isDragging, setIsDragging] = useState(false);
  const [isAtBottom, setIsAtBottom] = useState(true);
  const [showScrollButton, setShowScrollButton] = useState(false);
  const [stopping, setStopping] = useState(false);

  const assistantFileMatchesRef = useRef<Record<string, FileInfo>>({});
  const assistantFileMissRoundCountsRef = useRef<Record<string, number>>({});
  const pendingAssistantFileRoundCountsRef = useRef<Record<string, number>>({});
  const messagesEndRef = useRef<HTMLDivElement>(null);
  const chatAreaRef = useRef<HTMLDivElement>(null);
  const chatPaneRef = useRef<HTMLDivElement>(null);
  const sessionFilesShellRef = useRef<HTMLDivElement>(null);
  const sessionFilesPaneRef = useRef<HTMLElement>(null);
  const chatScrollTopBeforeFilesRef = useRef<Record<string, number>>({});
  const focusBeforeFilesRef = useRef<Record<string, HTMLElement | null>>({});
  const previousFilesStateRef = useRef({ sessionId, isOpen: false });
  const previousMobileOverlayRef = useRef(false);
  const filePanelTargetNonceRef = useRef(0);
  const composerTextareaRef = useRef<HTMLTextAreaElement>(null);
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
  const currentPreferenceDraft = composerDrafts.preferenceDrafts[currentDraftKey]
    || emptyTurnPreferenceDraft();
  const currentReasoningDraft = composerDrafts.reasoningDrafts[currentDraftKey];
  const turnReasoning = currentReasoningDraft?.modelId === selectedModelId
    ? currentReasoningDraft.selection
    : defaultTurnReasoning(selectedModel);
  const input = currentMessageDraft.input;
  const attachedFiles = currentMessageDraft.attachedFiles;
  const currentDraftId = currentMessageDraft.draftId;
  const creatingCurrentDraft = creatingDraftId === currentDraftId;
  const uploadingCurrentDraft = uploadingDraftIds.has(currentDraftId);
  const displayError = localError || runtimeError;
  const hasActiveSlot = activeSlotSessionIds?.has(sessionId) ?? false;
  const sessionFilesState = sessionId
    ? sessionFilesStates[sessionId] ?? createSessionFilesViewState(defaultChatRatio)
    : createSessionFilesViewState(defaultChatRatio);
  const filesLayout = sessionFilesState.layout;
  const chatRatio = sessionFilesState.chatRatio;
  const isFilesOpen = filesLayout !== 'closed';
  const isFilesExpanded = filesLayout === 'full';
  const isMobileFilesOverlay = isMobileFilesViewport && isFilesOpen;
  const chatInteractionHidden = isFilesExpanded || isMobileFilesOverlay;
  const lastRoundStatus = rounds[rounds.length - 1]?.status || 'empty';
  const filesRefreshNonce = `${rounds.length}:${lastRoundStatus}:${Number(sending)}:${Number(resuming)}`;
  const activeSlotSessionIdsRef = useRef(activeSlotSessionIds);
  const previousActiveSlotRef = useRef({ sessionId, hasActiveSlot });
  const committedFilesOwnerRef = useRef<SessionFileOwnerIdentity>({
    ownerSessionId: sessionId,
    ownerEpoch: 0,
  });
  const sessionFilesOwner = useMemo(() => {
    const previousFilesOwner = committedFilesOwnerRef.current;
    return previousFilesOwner.ownerSessionId === sessionId
      ? previousFilesOwner
      : { ownerSessionId: sessionId, ownerEpoch: previousFilesOwner.ownerEpoch + 1 };
  }, [sessionId]);
  const ownedFilePanelTarget = filePanelTarget
    && filePanelTarget.ownerSessionId === sessionFilesOwner.ownerSessionId
    && filePanelTarget.ownerEpoch === sessionFilesOwner.ownerEpoch
    ? filePanelTarget
    : null;

  sessionIdRef.current = sessionId;
  composerDraftsRef.current = composerDrafts;
  activeSlotSessionIdsRef.current = activeSlotSessionIds;

  useLayoutEffect(() => {
    committedFilesOwnerRef.current = sessionFilesOwner;
  }, [sessionFilesOwner]);

  useLayoutEffect(() => {
    onFilesFullChange?.(isFilesExpanded);
    return () => onFilesFullChange?.(false);
  }, [isFilesExpanded, onFilesFullChange]);

  const updateCurrentFilesState = (
    updater: (current: SessionFilesViewState) => SessionFilesViewState,
  ) => {
    if (!sessionId) return;
    setSessionFilesStates((previous) => {
      const current = previous[sessionId] ?? createSessionFilesViewState(defaultChatRatio);
      const next = updater(current);
      return next === current ? previous : { ...previous, [sessionId]: next };
    });
  };

  const setCurrentFilesLayout = (layout: SessionFilesLayout) => {
    updateCurrentFilesState((current) => {
      if (current.layout === layout) return current;
      return { ...current, layout };
    });
  };

  const openFilesPanel = () => {
    if (!sessionId) return;
    if (!isFilesOpen) {
      chatScrollTopBeforeFilesRef.current[sessionId] = chatAreaRef.current?.scrollTop ?? 0;
      focusBeforeFilesRef.current[sessionId] = document.activeElement instanceof HTMLElement
        ? document.activeElement
        : null;
    }
    const visibleRatio = isFilesOpen
      ? normalizeVisibleChatRatio(chatRatio)
      : DEFAULT_SESSION_FILES_CHAT_RATIO;
    updateCurrentFilesState((current) => (
      current.layout === 'split' && current.chatRatio === visibleRatio
        ? current
        : { ...current, layout: 'split', chatRatio: visibleRatio }
    ));
    if (visibleRatio !== chatRatio) {
      try {
        window.localStorage.setItem(SESSION_FILES_RATIO_STORAGE_KEY, String(visibleRatio));
      } catch {
        // The repaired ratio still applies for this page when storage is unavailable.
      }
    }
  };

  const closeFilesPanel = () => {
    setCurrentFilesLayout('closed');
    setFilePanelTarget(null);
  };

  useLayoutEffect(() => {
    const previous = previousFilesStateRef.current;
    if (
      previous.sessionId === sessionId
      && previous.isOpen
      && !isFilesOpen
      && chatAreaRef.current
    ) {
      chatAreaRef.current.scrollTop = chatScrollTopBeforeFilesRef.current[sessionId] ?? 0;
      const previousFocus = focusBeforeFilesRef.current[sessionId];
      const returnFocus = previousFocus?.isConnected
        ? previousFocus
        : chatPaneRef.current?.querySelector<HTMLElement>('[data-session-files-trigger="true"]');
      returnFocus?.focus({ preventScroll: true });
    }
    previousFilesStateRef.current = { sessionId, isOpen: isFilesOpen };
  }, [isFilesOpen, sessionId]);

  useLayoutEffect(() => {
    const wasMobileOverlay = previousMobileOverlayRef.current;
    if (!wasMobileOverlay && isMobileFilesOverlay) {
      sessionFilesPaneRef.current
        ?.querySelector<HTMLButtonElement>('button:not([disabled])')
        ?.focus({ preventScroll: true });
    }
    previousMobileOverlayRef.current = isMobileFilesOverlay;
  }, [isMobileFilesOverlay, sessionId]);

  useLayoutEffect(() => {
    const pane = chatPaneRef.current;
    if (!pane) return;
    if (chatInteractionHidden) pane.setAttribute('inert', '');
    else pane.removeAttribute('inert');
  }, [chatInteractionHidden]);

  useEffect(() => {
    if (typeof window.matchMedia !== 'function') return undefined;
    const mediaQuery = window.matchMedia(MOBILE_FILES_QUERY);
    const handleChange = (event: MediaQueryListEvent) => {
      setIsMobileFilesViewport(event.matches);
    };
    setIsMobileFilesViewport(mediaQuery.matches);
    mediaQuery.addEventListener('change', handleChange);
    return () => mediaQuery.removeEventListener('change', handleChange);
  }, []);

  const handleFilesRatioChange = (ratio: number) => {
    if (ratio <= 0) {
      setCurrentFilesLayout('full');
      return;
    }
    if (ratio >= 100) {
      closeFilesPanel();
      return;
    }
    updateCurrentFilesState((current) => (
      current.chatRatio === ratio && current.layout === 'split'
        ? current
        : { ...current, layout: 'split', chatRatio: ratio }
    ));
    try {
      window.localStorage.setItem(SESSION_FILES_RATIO_STORAGE_KEY, String(ratio));
    } catch {
      // The resize still applies for this page when persistent storage is unavailable.
    }
  };

  const toggleChatPane = () => {
    if (isFilesOpen) closeFilesPanel();
    else openFilesPanel();
  };

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
      const targetPreferences = previous.preferenceDrafts[targetSessionId];
      const targetMessageCompatible = !targetMessage
        || targetMessage.draftId === expectedDraftId
        || isPristineMessageDraft(targetMessage);
      const targetPreferencesCompatible = !targetPreferences
        || (
          targetPreferences.revision === 0
          && targetPreferences.skillKeys.length === 0
          && targetPreferences.mcpConnections.length === 0
        );
      if (!targetMessageCompatible || !targetPreferencesCompatible) return previous;

      const nextMessageDrafts = { ...previous.messageDrafts };
      nextMessageDrafts[targetSessionId] = targetMessage?.draftId === expectedDraftId
        && targetMessage.revision > sourceMessage.revision
        ? targetMessage
        : sourceMessage;
      delete nextMessageDrafts[sourceKey];

      const nextPreferenceDrafts = { ...previous.preferenceDrafts };
      const sourcePreferences = nextPreferenceDrafts[sourceKey];
      if (sourcePreferences) nextPreferenceDrafts[targetSessionId] = sourcePreferences;
      delete nextPreferenceDrafts[sourceKey];

      const nextReasoningDrafts = { ...previous.reasoningDrafts };
      const sourceReasoning = nextReasoningDrafts[sourceKey];
      if (sourceReasoning) nextReasoningDrafts[targetSessionId] = sourceReasoning;
      delete nextReasoningDrafts[sourceKey];

      return {
        messageDrafts: nextMessageDrafts,
        preferenceDrafts: nextPreferenceDrafts,
        reasoningDrafts: nextReasoningDrafts,
      };
    });
  };

  useEffect(() => {
    if (!sessionId) {
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
      setFilePanelTarget(null);
      resetAssistantFileMatches();
      setIsDragging(false);
      setDisableInitialMotion(false);
      setHighlightedRoundId(null);
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
    setFilePanelTarget(null);
    resetAssistantFileMatches();
    setIsAtBottom(false);
    prevRoundsLengthRef.current = 0;
    setLocalError('');
    setStopping(false);
    void loadSessionHistory(sessionId, {
      hasActiveSlot,
      isActiveSlotCurrent: () => (
        activeSlotSessionIdsRef.current?.has(sessionId) ?? false
      ),
    });
  }, [loadSessionHistory, sessionId]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    const previous = previousActiveSlotRef.current;
    previousActiveSlotRef.current = { sessionId, hasActiveSlot };
    if (
      !sessionId
      || previous.sessionId !== sessionId
      || previous.hasActiveSlot === hasActiveSlot
    ) {
      return;
    }
    if (!previous.hasActiveSlot && hasActiveSlot && hasLocalActiveTransport) {
      return;
    }

    // A snapshot-owned slot starts/restarts init discovery for an already open
    // session; a local direct/resume transport already owns that discovery.
    // A removed slot cancels the stale timer but still performs one final read
    // so a just-entered waiting_interaction Round can surface.
    void loadSessionHistory(sessionId, {
      hasActiveSlot,
      isActiveSlotCurrent: () => (
        activeSlotSessionIdsRef.current?.has(sessionId) ?? false
      ),
    });
  }, [hasActiveSlot, hasLocalActiveTransport, loadSessionHistory, sessionId]);

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
  }, [scrollTarget, sessionId, loading, rounds]);

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

  const handleOpenAssistantFile = (file: FileInfo) => {
    if (!sessionId) return;
    setLocalError('');
    const normalizedFile = toFileInfo(file, sessionId);
    const targetPath = normalizeAssistantTargetPath(normalizedFile.path);
    filePanelTargetNonceRef.current += 1;
    setFilePanelTarget({
      ...sessionFilesOwner,
      file: {
        ...normalizedFile,
        path: targetPath,
        session_id: sessionId,
      },
      nonce: filePanelTargetNonceRef.current,
    });
    openFilesPanel();
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
      const current = previous.preferenceDrafts[draftKey] || emptyTurnPreferenceDraft();
      return {
        ...previous,
        preferenceDrafts: {
          ...previous.preferenceDrafts,
          [draftKey]: {
            ...current,
            skillKeys: keys,
            revision: current.revision + 1,
          },
        },
      };
    });
  };

  const handleSelectedMcpConnectionsChange = (
    connections: PreferredMcpConnectionSnapshot[],
  ) => {
    const draftKey = currentDraftKey;
    setComposerDrafts((previous) => {
      const current = previous.preferenceDrafts[draftKey] || emptyTurnPreferenceDraft();
      return {
        ...previous,
        preferenceDrafts: {
          ...previous.preferenceDrafts,
          [draftKey]: {
            ...current,
            mcpConnections: connections.map((connection) => ({ ...connection })),
            revision: current.revision + 1,
          },
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
    const preferenceSnapshot: TurnPreferenceDraft = {
      skillKeys: [...currentPreferenceDraft.skillKeys],
      mcpConnections: currentPreferenceDraft.mcpConnections.map(
        (connection) => ({ ...connection }),
      ),
      revision: currentPreferenceDraft.revision,
    };
    const reasoningSnapshot = turnReasoning
      ? { ...turnReasoning }
      : null;
    const clearedMessageRevision = messageSnapshot.revision + 1;
    const clearedPreferenceRevision = preferenceSnapshot.revision + 1;
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
          preferenceDrafts: restoreFailedTurnPreferenceDraft(
            previous.preferenceDrafts,
            restoreDraftKey,
            preferenceSnapshot,
            clearedPreferenceRevision,
          ),
          reasoningDrafts: previous.reasoningDrafts,
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
          preferenceDrafts: {
            ...previous.preferenceDrafts,
            [initialSessionKey]: {
              skillKeys: [],
              mcpConnections: [],
              revision: clearedPreferenceRevision,
            },
          },
          reasoningDrafts: previous.reasoningDrafts,
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
        preferredSkillKeys: preferenceSnapshot.skillKeys,
        preferredMcpConnections: preferenceSnapshot.mcpConnections,
        reasoning: reasoningSnapshot || undefined,
        onRejectedBeforeAccept: restoreSubmissionSnapshot,
      });
      await sendPromise;
    } finally {
      pendingSendSessionKeysRef.current.delete(initialSessionKey);
      pendingSendSessionKeysRef.current.delete(targetSessionKey);
    }
  };

  const handleStop = async () => {
    if (!sessionId || !(sending || resuming || waitingInteraction) || stopping) return;
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

  const inputDisabled = sending || creatingCurrentDraft || resuming || waitingInteraction;
  const sendingLabel = creatingCurrentDraft
    ? '创建中'
    : resuming
      ? 'Resuming'
      : sending
        ? 'Running'
        : waitingInteraction
          ? 'Waiting'
          : '';
  const hasLiveReplyBelow = showScrollButton && (sending || resuming);

  return (
    <div
      ref={sessionFilesShellRef}
      className="session-files-shell relative flex h-screen flex-1 bg-claude-bg"
      data-layout={filesLayout}
      style={{ '--session-files-chat-ratio': `${chatRatio}%` } as CSSProperties}
    >
      <div
        ref={chatPaneRef}
        className="session-files-chat-pane relative flex flex-col"
        data-testid="chat-pane"
        aria-hidden={chatInteractionHidden}
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

        <header
          data-testid="chat-toolbar"
          className="sticky top-0 z-20 flex h-14 shrink-0 items-center border-b border-claude-border bg-claude-bg/80 px-6 backdrop-blur-sm"
        >
          <div className="ml-auto flex items-center gap-1">
            {sessionId && (
              <SessionFilesButton
                open={isFilesOpen}
                onToggle={openFilesPanel}
              />
            )}
            {sessionId && !isFilesExpanded && !isMobileFilesViewport && (
              <ChatPaneButton
                filesOpen={isFilesOpen}
                onToggle={toggleChatPane}
              />
            )}
          </div>
        </header>

        <div ref={chatAreaRef} className="relative flex-1 overflow-y-auto bg-claude-bg">
          {loading && rounds.length === 0 ? (
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
            <div data-testid="chat-message-column" className="mx-auto w-full max-w-5xl space-y-6 px-4 py-6 md:px-8">
              {rounds.map((round, index) => (
                <div
                  key={round.round_id}
                  ref={(el) => {
                    roundElementRefs.current[round.round_id] = el;
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
                    isStreaming={(sending || resuming) && index === rounds.length - 1}
                    disableMotion={disableInitialMotion}
                  />
                </div>
              ))}
              <div ref={messagesEndRef} />
            </div>
          )}

          {showScrollButton && (
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
            <div className="mx-auto w-full max-w-5xl">
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

        {pendingInterrupt && pendingInterrupt.reason === 'input_required' && pendingInterrupt.payload?.questions && (pendingInterrupt.payload.questions as AskUserQuestion[]).length > 0 && (
          <div className="relative z-20 mx-auto mb-[-3.5rem] w-full max-w-5xl px-4 md:px-8">
            <QuestionCard
              key={pendingInterrupt.id}
              questions={pendingInterrupt.payload.questions as AskUserQuestion[]}
              onSubmit={handleResumeSubmit}
              disabled={resuming}
            />
          </div>
        )}

        {pendingInterrupt && pendingInterrupt.reason === 'human_approval' && pendingInterrupt.payload?.kind === 'tool_approval' && (
          <div className="relative z-20 mx-auto mb-[-3.5rem] w-full max-w-5xl px-4 md:px-8">
            <ToolApprovalCard
              approval={pendingInterrupt.payload as ToolApprovalPayload}
              onSubmit={handleResumeSubmit}
              disabled={resuming}
            />
          </div>
        )}

        <ChatInput
            textareaRef={composerTextareaRef}
            value={input}
            onChange={(value) => updateMessageDraft(currentDraftKey, (draft) => (
              draft.input === value
                ? draft
                : { ...draft, input: value, revision: draft.revision + 1 }
            ))}
            onSend={handleSend}
            onStop={(sending || resuming || waitingInteraction) ? handleStop : undefined}
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
            selectedSkillKeys={currentPreferenceDraft.skillKeys}
            onSelectedSkillKeysChange={handleSelectedSkillKeysChange}
            selectedMcpConnections={currentPreferenceDraft.mcpConnections}
            onSelectedMcpConnectionsChange={handleSelectedMcpConnectionsChange}
            modelControl={(
              <ModelSelector
                selectedModelId={selectedModelId}
                onModelChange={(modelId) => {
                  const model = availableModels.find((item) => item.id === modelId);
                  setComposerDrafts((previous) => ({
                    ...previous,
                    reasoningDrafts: {
                      ...previous.reasoningDrafts,
                      [currentDraftKey]: {
                        modelId,
                        selection: defaultTurnReasoning(model),
                      },
                    },
                  }));
                  onModelChange(modelId);
                }}
                availableModels={availableModels}
                reasoningSelection={turnReasoning}
                onSelectionComplete={() => composerTextareaRef.current?.focus()}
                onReasoningChange={(selection) => {
                  setComposerDrafts((previous) => ({
                    ...previous,
                    reasoningDrafts: {
                      ...previous.reasoningDrafts,
                      [currentDraftKey]: { modelId: selectedModelId, selection },
                    },
                  }));
                }}
                readOnly={!!sessionId}
              />
            )}
          />
      </div>

      {sessionId && !isMobileFilesViewport && (
        <SessionFilesSplitter
          containerRef={sessionFilesShellRef}
          chatRatio={filesLayout === 'full' ? 0 : filesLayout === 'closed' ? 100 : chatRatio}
          onRatioChange={handleFilesRatioChange}
          onStartEdgeCollapse={onStartEdgeCollapseSidebar}
        />
      )}

      <aside
        ref={sessionFilesPaneRef}
        className="session-files-pane min-h-0 bg-white"
        data-testid="session-files-pane"
        aria-label="会话文件"
        aria-hidden={!isFilesOpen}
      >
        <ArtifactsPanel
          ref={sessionFilesHandleRef}
          sessionId={sessionId}
          ownerEpoch={sessionFilesOwner.ownerEpoch}
          isOpen={isFilesOpen}
          onClose={closeFilesPanel}
          targetFile={ownedFilePanelTarget?.file || null}
          targetFileNonce={ownedFilePanelTarget?.nonce}
          refreshNonce={filesRefreshNonce}
          variant="workspace"
          isExpanded={isFilesExpanded}
          onToggleExpanded={() => {
            setCurrentFilesLayout(isFilesExpanded ? 'split' : 'full');
          }}
        />
      </aside>

      {previewFile && previewSessionId && (
        <FilePreview
          sessionId={previewSessionId}
          file={previewFile}
          readOnly
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
