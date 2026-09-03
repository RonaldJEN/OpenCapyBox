import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  useSyncExternalStore,
  type CSSProperties,
  type Ref,
} from 'react';

import { apiService } from '../services/api';
import {
  AttachmentInfo,
  AskUserQuestion,
  ChatFile,
  ChatContentBlock,
  FileInfo,
  ModelInfo,
  PreferredMcpConnectionSnapshot,
  PendingFileDraftInfo,
  ToolApprovalPayload,
  TurnReasoningSelection,
} from '../types';
import type { WorkspaceEntry } from '../types/workspace';
import {
  getWorkspaceProjectionRevision,
  isWorkspaceEntryDeleted,
  requestOpenWorkspace,
  subscribeWorkspaceProjection,
} from '../services/workspaceEvents';
import { workspaceApi } from '../services/workspaceApi';
import {
  flushWorkspaceDraft,
  getWorkspaceDraft,
  workspaceDraftKey,
} from '../services/workspaceDraftOutbox';
import {
  ChatRuntimeProvider,
  useChatRuntime,
  useChatRuntimeOptional,
} from '../runtime/ChatRuntimeProvider';
import { readFileAsDataUrl } from '../utils/imageUtils';
import { toFileInfo, isImageFile } from '../utils/fileUtils';
import {
  emptyTurnPreferenceDraft,
  type TurnPreferenceDraft,
} from '../utils/turnPreferenceDrafts';
import {
  MAX_TEXT_BLOCK_CHARS,
  formatUploadError,
  messageTooLongText,
} from '../utils/errorMessages';
import { Round } from './Round';
import { ArtifactsPanel, type ArtifactsPanelHandle } from './ArtifactsPanel';
import { type SessionFileOwnerIdentity } from './FilePreview';
import { ModelSelector } from './ModelSelector';
import { ChatInput } from './ChatInput';
import { QuestionCard } from './QuestionCard';
import { ToolApprovalCard } from './ToolApprovalCard';
import {
  ChatPaneButton,
  SessionFilesButton,
} from './session-files/SessionFilesControls';
import { SessionFilesSplitter } from './session-files/SessionFilesSplitter';
import {
  WorkspaceFilesPanel,
  type WorkspaceFilesPanelHandle,
} from './workspace/WorkspaceFilesPanel';
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
  attachedFiles: ChatFile[];
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
  workspaceFilesHandleRef?: Ref<WorkspaceFilesPanelHandle>;
  workspaceFileTarget?: WorkspaceEntry | null;
  workspaceTargetResolving?: boolean;
  onWorkspaceFilesClose?: () => void;
  onWorkspaceTabSelect?: (entry: WorkspaceEntry, options?: { replace?: boolean }) => void;
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
    sessionFilesHandleRef: externalSessionFilesHandleRef,
    workspaceFilesHandleRef: externalWorkspaceFilesHandleRef,
    workspaceFileTarget = null,
    workspaceTargetResolving = false,
    onWorkspaceFilesClose,
    onWorkspaceTabSelect,
    onStartEdgeCollapseSidebar,
    activeSlotSessionIds,
    scrollTarget,
  } = props;
  const runtime = useChatRuntime();
  const loadSessionHistory = runtime.loadSessionHistory;
  const projection = runtime.getSessionProjection(sessionId);
  const rounds = projection.rounds;
  useSyncExternalStore(
    subscribeWorkspaceProjection,
    getWorkspaceProjectionRevision,
    getWorkspaceProjectionRevision,
  );
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
  const [syncingAttachmentDraftId, setSyncingAttachmentDraftId] = useState<string | null>(null);
  const [defaultChatRatio] = useState(readInitialChatRatio);
  const [sessionFilesStates, setSessionFilesStates] = useState<Record<string, SessionFilesViewState>>({});
  const [workspaceFilesState, setWorkspaceFilesState] = useState<SessionFilesViewState>(() => createSessionFilesViewState(defaultChatRatio));
  const [filePanelTarget, setFilePanelTarget] = useState<(
    SessionFileOwnerIdentity & { file: FileInfo; nonce: number }
  ) | null>(null);
  const [previewContextNotice, setPreviewContextNotice] = useState('');
  const [uploadingDraftIds, setUploadingDraftIds] = useState<Set<string>>(new Set());
  const [isDragging, setIsDragging] = useState(false);
  const [isAtBottom, setIsAtBottom] = useState(true);
  const [showScrollButton, setShowScrollButton] = useState(false);
  const [stopping, setStopping] = useState(false);

  const messagesEndRef = useRef<HTMLDivElement>(null);
  const chatAreaRef = useRef<HTMLDivElement>(null);
  const chatPaneRef = useRef<HTMLDivElement>(null);
  const sessionFilesShellRef = useRef<HTMLDivElement>(null);
  const sessionFilesPaneRef = useRef<HTMLElement>(null);
  const sessionFilesHandleRef = useRef<ArtifactsPanelHandle | null>(null);
  const setSessionFilesHandle = useCallback((handle: ArtifactsPanelHandle | null) => {
    sessionFilesHandleRef.current = handle;
    if (typeof externalSessionFilesHandleRef === 'function') externalSessionFilesHandleRef(handle);
    else if (externalSessionFilesHandleRef) {
      (externalSessionFilesHandleRef as { current: ArtifactsPanelHandle | null }).current = handle;
    }
  }, [externalSessionFilesHandleRef]);
  const workspaceFilesHandleRef = useRef<WorkspaceFilesPanelHandle | null>(null);
  const setWorkspaceFilesHandle = useCallback((handle: WorkspaceFilesPanelHandle | null) => {
    workspaceFilesHandleRef.current = handle;
    if (typeof externalWorkspaceFilesHandleRef === 'function') externalWorkspaceFilesHandleRef(handle);
    else if (externalWorkspaceFilesHandleRef) {
      (externalWorkspaceFilesHandleRef as { current: WorkspaceFilesPanelHandle | null }).current = handle;
    }
  }, [externalWorkspaceFilesHandleRef]);
  const chatScrollTopBeforeFilesRef = useRef<Record<string, number>>({});
  const focusBeforeFilesRef = useRef<Record<string, HTMLElement | null>>({});
  const previousFilesStateRef = useRef({ sessionId, isOpen: false });
  const filePanelTargetNonceRef = useRef(0);
  const attachmentPreviewRequestIdRef = useRef(0);
  const assistantFileOpenRequestIdRef = useRef(0);
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
  const resumeSaveBarrierPendingRef = useRef(false);
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
  const turnReasoning = currentReasoningDraft && currentReasoningDraft.modelId === selectedModelId
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
  const workspacePanelActive = Boolean(workspaceFileTarget);
  const activeFilesState = workspacePanelActive ? workspaceFilesState : sessionFilesState;
  const filesLayout = activeFilesState.layout;
  const chatRatio = activeFilesState.chatRatio;
  const isFilesOpen = filesLayout !== 'closed';
  const isFilesExpanded = filesLayout === 'full';
  const chatInteractionHidden = isFilesExpanded;
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

  const commitComposerDrafts = (
    updater: (current: ComposerDraftState) => ComposerDraftState,
  ) => {
    const previous = composerDraftsRef.current;
    const next = updater(previous);
    if (next === previous) return;
    composerDraftsRef.current = next;
    setComposerDrafts(next);
  };

  useLayoutEffect(() => {
    committedFilesOwnerRef.current = sessionFilesOwner;
  }, [sessionFilesOwner]);

  useLayoutEffect(() => {
    if (!workspaceFileTarget || workspaceFilesState.layout !== 'closed') return;
    chatScrollTopBeforeFilesRef.current[sessionId] = chatAreaRef.current?.scrollTop ?? 0;
    focusBeforeFilesRef.current[sessionId] = document.activeElement instanceof HTMLElement
      ? document.activeElement
      : null;
    setWorkspaceFilesState((current) => ({
      ...current,
      layout: 'split',
      chatRatio: normalizeVisibleChatRatio(current.chatRatio),
    }));
  }, [sessionId, workspaceFileTarget, workspaceFilesState.layout]);

  useLayoutEffect(() => {
    onFilesFullChange?.(chatInteractionHidden);
    return () => onFilesFullChange?.(false);
  }, [chatInteractionHidden, onFilesFullChange]);

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
    if (workspacePanelActive) {
      setWorkspaceFilesState((current) => current.layout === layout ? current : { ...current, layout });
      return;
    }
    updateCurrentFilesState((current) => {
      if (current.layout === layout) return current;
      return { ...current, layout };
    });
  };

  const saveWorkspaceFilesInBackground = () => {
    const workspaceHandle = workspaceFilesHandleRef.current;
    if (!workspaceHandle?.hasDirty()) return;
    void workspaceHandle.saveDirty().catch((error) => {
      console.error('Failed to sync workspace files in background:', error);
    });
  };

  const captureDirtyEditorsBeforeAgentStart = useCallback((): PendingFileDraftInfo[] => {
    const pending: PendingFileDraftInfo[] = [];
    const sessionHandle = sessionFilesHandleRef.current;
    if (sessionHandle && sessionId) {
      const ownerMatches = sessionHandle.ownerSessionId === sessionFilesOwner.ownerSessionId
        && sessionHandle.ownerEpoch === sessionFilesOwner.ownerEpoch;
      if (ownerMatches && sessionHandle.hasDirty(sessionFilesOwner)) {
        pending.push(...sessionHandle.pendingFileDrafts(sessionFilesOwner));
        try {
          void sessionHandle.saveDirty(sessionFilesOwner).catch((error) => {
            console.error('Failed to sync Session drafts in background:', error);
          });
        } catch (error) {
          console.error('Failed to capture Session drafts:', error);
        }
      }
    }
    const workspaceHandle = workspaceFilesHandleRef.current;
    if (workspaceHandle?.hasDirty()) {
      pending.push(...workspaceHandle.pendingFileDrafts());
      try {
        void workspaceHandle.saveDirty().catch((error) => {
          console.error('Failed to sync Workspace drafts in background:', error);
        });
      } catch (error) {
        console.error('Failed to capture Workspace drafts:', error);
      }
    }
    return pending.filter((item, index, items) => (
      items.findIndex((candidate) => candidate.source === item.source && candidate.path === item.path) === index
    ));
  }, [sessionFilesOwner, sessionId]);

  const openFilesPanel = () => {
    if (!sessionId) return;
    if (workspacePanelActive) {
      saveWorkspaceFilesInBackground();
      onWorkspaceFilesClose?.();
    }
    if (sessionFilesState.layout === 'closed') {
      chatScrollTopBeforeFilesRef.current[sessionId] = chatAreaRef.current?.scrollTop ?? 0;
      focusBeforeFilesRef.current[sessionId] = document.activeElement instanceof HTMLElement
        ? document.activeElement
        : null;
    }
    const visibleRatio = sessionFilesState.layout !== 'closed'
      ? normalizeVisibleChatRatio(sessionFilesState.chatRatio)
      : DEFAULT_SESSION_FILES_CHAT_RATIO;
    updateCurrentFilesState((current) => (
      current.layout === 'split' && current.chatRatio === visibleRatio
        ? current
        : { ...current, layout: 'split', chatRatio: visibleRatio }
    ));
    if (visibleRatio !== sessionFilesState.chatRatio) {
      try {
        window.localStorage.setItem(SESSION_FILES_RATIO_STORAGE_KEY, String(visibleRatio));
      } catch {
        // The repaired ratio still applies for this page when storage is unavailable.
      }
    }
  };

  const finalizeWorkspacePanelClose = () => {
    setWorkspaceFilesState((current) => ({ ...current, layout: 'closed' }));
    onWorkspaceFilesClose?.();
  };

  const closeFilesPanel = () => {
    if (workspacePanelActive) {
      saveWorkspaceFilesInBackground();
      finalizeWorkspacePanelClose();
      return;
    }
    const sessionHandle = sessionFilesHandleRef.current;
    if (sessionHandle?.hasDirty(sessionFilesOwner)) {
      void sessionHandle.saveDirty(sessionFilesOwner).catch((error) => {
        console.error('Failed to sync Session drafts on close:', error);
      });
    }
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
    const pane = chatPaneRef.current;
    if (!pane) return;
    if (chatInteractionHidden) pane.setAttribute('inert', '');
    else pane.removeAttribute('inert');
  }, [chatInteractionHidden]);

  const handleFilesRatioChange = (ratio: number) => {
    if (ratio <= 0) {
      setCurrentFilesLayout('full');
      return;
    }
    if (ratio >= 100) {
      closeFilesPanel();
      return;
    }
    if (workspacePanelActive) {
      setWorkspaceFilesState((current) => current.chatRatio === ratio && current.layout === 'split'
        ? current
        : { ...current, layout: 'split', chatRatio: ratio });
    } else {
      updateCurrentFilesState((current) => (
        current.chatRatio === ratio && current.layout === 'split'
          ? current
          : { ...current, layout: 'split', chatRatio: ratio }
      ));
    }
    try {
      window.localStorage.setItem(SESSION_FILES_RATIO_STORAGE_KEY, String(ratio));
    } catch {
      // The resize still applies for this page when persistent storage is unavailable.
    }
  };

  const toggleChatPane = () => {
    if (isFilesOpen) closeFilesPanel();
    else void openFilesPanel();
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
    commitComposerDrafts((previous) => {
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

  const buildDisplayMessage = (text: string, files: ChatFile[]) => {
    const trimmed = text.trim();
    if (trimmed) return trimmed;
    return files.length > 0 ? '' : '[空消息]';
  };

  const buildContentBlocks = (text: string, files: ChatFile[]): ChatContentBlock[] => {
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

    const imageFiles = files.filter((file) => file.source !== 'workspace' && isImageFile(file));
    if (imageFiles.length > 0 && !(selectedModel?.supports_image ?? false)) {
      throw new Error(`当前模型 ${selectedModel?.name || selectedModelId} 不支持图片输入`);
    }
    if (imageFiles.length > 0 && imageFiles.length > (selectedModel?.max_images ?? 0)) {
      throw new Error(`当前模型最多支持 ${selectedModel?.max_images ?? 0} 张图片`);
    }

    for (const file of files) {
      if (file.source === 'workspace') {
        blocks.push({
          type: 'file',
          file: {
            source: 'workspace',
            entry_id: file.entry_id,
            ...(file.version_id ? { version_id: file.version_id } : {}),
            ...(file.is_directory ? { kind: 'directory' as const } : {}),
            name: file.name,
            mime_type: toMimeType(file.type),
            size: file.size,
          },
        });
        continue;
      }
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
            source: 'session',
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
      setPreviewContextNotice('');
      setFilePanelTarget(null);
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
    setPreviewContextNotice('');
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
    const uploadedFiles: ChatFile[] = [];
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
        const uploaded = await apiService.uploadFile(targetSessionId, file);
        const fileInfo: ChatFile = {
          source: 'session',
          name: uploaded.name,
          path: uploaded.path,
          revision: uploaded.revision,
          size: uploaded.size,
          modified: uploaded.modified,
          session_id: targetSessionId,
          type: file.type || uploaded.type,
          is_directory: uploaded.is_directory,
          data_url: uploaded.data_url,
        };
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

  const previewSessionAttachment = async (file: AttachmentInfo | FileInfo) => {
    const requestId = ++attachmentPreviewRequestIdRef.current;
    const targetSessionId = file.session_id || sessionId;
    if (!targetSessionId) {
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
    if (requestId !== attachmentPreviewRequestIdRef.current) return;
    if (normalizedFile.is_directory) {
      void handleOpenAssistantFile(normalizedFile);
      return;
    }
    filePanelTargetNonceRef.current += 1;
    setFilePanelTarget({
      ...sessionFilesOwner,
      file: {
        ...normalizedFile,
        session_id: targetSessionId,
        content_mode: 'captured',
      },
      nonce: filePanelTargetNonceRef.current,
    });
    setPreviewContextNotice('');
    openFilesPanel();
  };

  const handlePreviewDraftAttachment = async (file: AttachmentInfo | FileInfo) => {
    if (file.source === 'workspace' && file.entry_id) {
      requestOpenWorkspace(
        file.entry_id,
      );
      return;
    }
    await previewSessionAttachment(file);
  };

  const handlePreviewHistoryAttachment = async (file: AttachmentInfo | FileInfo) => {
    if (file.source === 'workspace') {
      if (isWorkspaceEntryDeleted(file.entry_id)) {
        setLocalError('工作区文件已永久删除，历史中不再保留可读取副本。');
        return;
      }
      if (file.is_directory && file.entry_id) {
        setLocalError('');
        requestOpenWorkspace(file.entry_id);
        return;
      }
      const capturedPath = (file.snapshot_path || file.path).replace(/^\/+/, '');
      if (!capturedPath.startsWith('.workspace-snapshots/')) {
        setLocalError('工作区附件正在准备，请稍后再打开。');
        return;
      }
    }
    setLocalError('');
    await previewSessionAttachment(file);
  };

  const handleOpenAssistantFile = async (file: FileInfo) => {
    if (!sessionId) return;
    const requestId = ++assistantFileOpenRequestIdRef.current;
    const ownerSessionId = sessionId;
    setLocalError('');
    setPreviewContextNotice('');
    const normalizedFile = toFileInfo(file, sessionId);

    const showCapturedFallback = (message: string) => {
      if (
        requestId !== assistantFileOpenRequestIdRef.current
        || sessionIdRef.current !== ownerSessionId
      ) return;
      if (normalizedFile.source === 'workspace') {
        setLocalError('工作区文件已删除或暂时无法读取；已删除的文件不保留历史副本。');
        return;
      }
      const capturedSessionId = normalizedFile.session_id || ownerSessionId;
      if (!normalizedFile.snapshot_path) {
        setLocalError('当前会话文件已不存在，且没有可验证的生成时版本。');
        return;
      }
      filePanelTargetNonceRef.current += 1;
      setFilePanelTarget({
        ...sessionFilesOwner,
        file: {
          ...normalizedFile,
          path: normalizedFile.snapshot_path,
          session_id: capturedSessionId,
          content_mode: 'captured',
        },
        nonce: filePanelTargetNonceRef.current,
      });
      setPreviewContextNotice(message);
      openFilesPanel();
    };

    if (normalizedFile.assistant_ref_id && normalizedFile.source === 'workspace') {
      if (!normalizedFile.entry_id) {
        showCapturedFallback('当前工作区文件身份无效，正在显示生成时版本。');
        return;
      }
      try {
        const currentEntry = await workspaceApi.getEntry(normalizedFile.entry_id);
        if (
          requestId !== assistantFileOpenRequestIdRef.current
          || sessionIdRef.current !== ownerSessionId
        ) return;
        setPreviewContextNotice('');
        requestOpenWorkspace(currentEntry.entry_id);
      } catch (error) {
        showCapturedFallback(
          (error as { status?: unknown })?.status === 404
            ? '工作区文件已删除，无法恢复。'
            : '工作区文件暂时无法读取，请稍后重试。',
        );
      }
      return;
    }

    if (normalizedFile.assistant_ref_id && normalizedFile.source !== 'workspace') {
      const targetSessionId = normalizedFile.session_id || ownerSessionId;
      const targetPath = normalizedFile.path.replace(/^\/+/, '');
      const parentPath = targetPath.includes('/')
        ? targetPath.slice(0, targetPath.lastIndexOf('/'))
        : undefined;
      try {
        const listed = await apiService.getSessionFiles(targetSessionId, parentPath);
        const currentFile = listed.files.find((item) => (
          !item.is_directory && item.path.replace(/^\/+/, '') === targetPath
        ));
        if (
          requestId !== assistantFileOpenRequestIdRef.current
          || sessionIdRef.current !== ownerSessionId
        ) return;
        if (!currentFile) {
          showCapturedFallback('当前会话文件已删除，正在显示生成时版本。');
          return;
        }
        filePanelTargetNonceRef.current += 1;
        setFilePanelTarget({
          ...sessionFilesOwner,
          file: {
            ...currentFile,
            source: 'session',
            session_id: targetSessionId,
            content_mode: 'current',
            assistant_ref_id: normalizedFile.assistant_ref_id,
            snapshot_path: normalizedFile.snapshot_path,
          },
          nonce: filePanelTargetNonceRef.current,
        });
        setPreviewContextNotice('');
        openFilesPanel();
      } catch {
        showCapturedFallback('当前会话文件无法读取，正在显示生成时版本。');
      }
      return;
    }

    const targetPath = normalizedFile.path.replace(/^\/+/, '');
    setPreviewContextNotice('');
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
      const normalizedFile: ChatFile = {
        source: 'session',
        name: file.name,
        path: file.path,
        revision: file.revision,
        size: file.size,
        modified: file.modified,
        type: file.type,
        is_directory: file.is_directory,
        data_url: file.data_url,
        session_id: file.session_id || sessionId || undefined,
      };
      if (draft.attachedFiles.some((item) => (
        item.source !== 'workspace'
        && item.path === normalizedFile.path
        && item.session_id === normalizedFile.session_id
      ))) return draft;
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

  const handleWorkspaceFilesSelected = (entries: WorkspaceEntry[]) => {
    updateMessageDraft(currentDraftKey, (draft) => {
      const existingEntryIds = new Set(
        draft.attachedFiles
          .filter((file) => file.source === 'workspace')
          .map((file) => file.entry_id),
      );
      const additions: ChatFile[] = entries
        .filter((entry) => !existingEntryIds.has(entry.entry_id))
        .map((entry) => ({
          source: 'workspace',
          entry_id: entry.entry_id,
          revision: entry.revision,
          tree_revision: entry.tree_revision,
          workspace_path: entry.path,
          path: entry.path,
          name: entry.name,
          size: entry.size_bytes,
          modified: entry.updated_at,
          type: entry.kind === 'directory'
            ? 'inode/directory'
            : entry.mime_type || entry.name.split('.').pop()?.toLowerCase() || 'file',
          is_directory: entry.kind === 'directory',
        }));
      if (additions.length === 0) return draft;
      return {
        ...draft,
        revision: draft.revision + 1,
        attachedFiles: [...draft.attachedFiles, ...additions],
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
    let submissionRestored = false;
    const restoreSubmissionSnapshot = () => {
      if (submissionRestored) return;
      submissionRestored = true;
      commitComposerDrafts((previous) => {
        const current = previous.messageDrafts[restoreDraftKey];
        if (
          current
          && current.draftId !== messageSnapshot.draftId
          && !isPristineMessageDraft(current)
        ) return previous;
        const preferenceRevision = previous.preferenceDrafts[restoreDraftKey]?.revision
          ?? clearedPreferenceRevision;
        return {
          messageDrafts: {
            ...previous.messageDrafts,
            [restoreDraftKey]: {
              ...messageSnapshot,
              revision: Math.max(
                current?.revision ?? clearedMessageRevision,
                clearedMessageRevision,
              ) + 1,
              attachedFiles: [...messageSnapshot.attachedFiles],
            },
          },
          preferenceDrafts: {
            ...previous.preferenceDrafts,
            [restoreDraftKey]: {
              ...preferenceSnapshot,
              skillKeys: [...preferenceSnapshot.skillKeys],
              mcpConnections: preferenceSnapshot.mcpConnections.map(
                (connection) => ({ ...connection }),
              ),
              revision: preferenceRevision + 1,
            },
          },
          reasoningDrafts: reasoningSnapshot
            ? {
              ...previous.reasoningDrafts,
              [restoreDraftKey]: {
                modelId: selectedModelId,
                selection: reasoningSnapshot,
              },
            }
            : previous.reasoningDrafts,
        };
      });
    };
    pendingSendSessionKeysRef.current.add(initialSessionKey);
    let targetSessionKey = initialSessionKey;
    try {
      const attachedWorkspaceEntryIds = Array.from(new Set(
        draftAttachments.flatMap((file) => (
          file.source === 'workspace' && file.entry_id ? [file.entry_id] : []
        )),
      ));
      if (attachedWorkspaceEntryIds.length > 0) {
        setSyncingAttachmentDraftId(capturedDraftId);
        const saveResult = await workspaceFilesHandleRef.current?.saveEntries(
          attachedWorkspaceEntryIds,
        );
        if (saveResult && !saveResult.ok) {
          setLocalError('附件尚未同步完成，请确认文件保存成功后重试');
          return;
        }
        try {
          await Promise.all(attachedWorkspaceEntryIds.map(async (entryId) => {
            if (await getWorkspaceDraft(entryId)) {
              await flushWorkspaceDraft(workspaceDraftKey(entryId));
            }
          }));
        } catch {
          setLocalError('附件尚未同步完成，请确认文件保存成功后重试');
          return;
        }
      }

      let contentBlocks: ChatContentBlock[] = [];
      try {
        contentBlocks = buildContentBlocks(draftInput, draftAttachments);
      } catch (err: any) {
        setLocalError(err?.message || '消息构建失败');
        return;
      }

      const pendingFileDrafts = captureDirtyEditorsBeforeAgentStart();
      const attachedWorkspacePaths = new Set(
        draftAttachments.flatMap((file) => (
          file.source === 'workspace' && file.workspace_path ? [file.workspace_path] : []
        )),
      );
      if (pendingFileDrafts.some((item) => (
        item.source === 'workspace' && attachedWorkspacePaths.has(item.path)
      ))) {
        setLocalError('附件在同步期间又发生了修改，请保存完成后重试');
        return;
      }
      const userMessage = buildDisplayMessage(draftInput, draftAttachments);
      const isStartingNewSession = !sessionId;
      commitComposerDrafts((previous) => {
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
        pendingFileDrafts,
        onRejectedBeforeAccept: restoreSubmissionSnapshot,
      });
      await sendPromise;
    } finally {
      setSyncingAttachmentDraftId((current) => current === capturedDraftId ? null : current);
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
    if (!sessionId || !pendingInterrupt?.id || resumeSaveBarrierPendingRef.current) return;
    resumeSaveBarrierPendingRef.current = true;
    try {
      const pendingFileDrafts = captureDirtyEditorsBeforeAgentStart();
      await runtime.resumeRun(sessionId, pendingInterrupt, answers, pendingFileDrafts);
    } finally {
      resumeSaveBarrierPendingRef.current = false;
    }
  };

  const syncingCurrentAttachments = syncingAttachmentDraftId === currentDraftId;
  const inputDisabled = sending || creatingCurrentDraft || syncingCurrentAttachments || resuming || waitingInteraction;
  const sendingLabel = creatingCurrentDraft
    ? '创建中'
    : syncingCurrentAttachments
      ? '同步附件'
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
                open={!workspacePanelActive && isFilesOpen}
                onToggle={() => void openFilesPanel()}
              />
            )}
            {(sessionId || workspacePanelActive) && !isFilesExpanded && (
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
              {rounds.map((round, index) => {
                const visibleUserAttachments = (round.user_attachments || []).filter((file) => (
                  file.source !== 'workspace' || !isWorkspaceEntryDeleted(file.entry_id)
                ));
                const visibleAssistantFileReferences = (round.assistant_file_references || []).filter((file) => (
                  file.source !== 'workspace' || !isWorkspaceEntryDeleted(file.entry_id)
                ));
                const visibleRound = visibleAssistantFileReferences.length === (round.assistant_file_references || []).length
                  ? round
                  : { ...round, assistant_file_references: visibleAssistantFileReferences };
                return (
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
                      round={visibleRound}
                      userAttachments={visibleUserAttachments}
                      sessionId={sessionId}
                      onPreviewAttachment={handlePreviewHistoryAttachment}
                      onOpenFileInPanel={handleOpenAssistantFile}
                      isStreaming={(sending || resuming) && index === rounds.length - 1}
                      disableMotion={disableInitialMotion}
                    />
                  </div>
                );
              })}
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
            onWorkspaceFilesSelected={handleWorkspaceFilesSelected}
            onInputDropHandled={() => setIsDragging(false)}
            onPreviewAttachment={handlePreviewDraftAttachment}
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

      {(sessionId || workspacePanelActive) && (
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
        aria-label={workspacePanelActive ? '工作区文件' : '会话文件'}
        aria-hidden={!isFilesOpen}
      >
        {workspacePanelActive ? (
          <WorkspaceFilesPanel
            ref={setWorkspaceFilesHandle}
            target={workspaceFileTarget}
            onActivateEntry={onWorkspaceTabSelect}
            resolvingTarget={workspaceTargetResolving}
            isOpen={isFilesOpen}
            onClose={() => {
              setPreviewContextNotice('');
              closeFilesPanel();
            }}
            isExpanded={isFilesExpanded}
            showExpandToggle
            onToggleExpanded={() => setCurrentFilesLayout(isFilesExpanded ? 'split' : 'full')}
          />
        ) : (
          <ArtifactsPanel
            ref={setSessionFilesHandle}
            sessionId={sessionId}
            ownerEpoch={sessionFilesOwner.ownerEpoch}
            isOpen={isFilesOpen}
            onClose={closeFilesPanel}
            targetFile={ownedFilePanelTarget?.file || null}
            targetFileNonce={ownedFilePanelTarget?.nonce}
            targetContextNotice={previewContextNotice}
            refreshNonce={filesRefreshNonce}
            variant="workspace"
            isExpanded={isFilesExpanded}
            onToggleExpanded={() => {
              setCurrentFilesLayout(isFilesExpanded ? 'split' : 'full');
            }}
          />
        )}
      </aside>
    </div>
  );
}
