import {
  forwardRef,
  useCallback,
  useEffect,
  useImperativeHandle,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type MouseEvent,
} from 'react';
import { formatDistanceToNow } from 'date-fns';
import { zhCN } from 'date-fns/locale/zh-CN';
import {
  ArrowUp,
  ChevronLeft,
  ChevronRight,
  Download,
  Folder,
  FolderOpen,
  FolderTree,
  Loader2,
  RotateCcw,
  X,
} from 'lucide-react';

import { apiService } from '../services/api';
import { FileInfo } from '../types';
import { getFileIcon, getFileIconClass } from '../utils/fileUtils';
import {
  FilePreview,
  type FilePreviewHandle,
  type SessionFileOwnerIdentity,
} from './FilePreview';
import { SessionFilesExpandButton } from './session-files/SessionFilesControls';

interface ArtifactsPanelProps {
  sessionId: string;
  ownerEpoch?: number;
  isOpen: boolean;
  onClose: () => void;
  targetFile?: FileInfo | null;
  targetFileNonce?: number;
  variant?: 'drawer' | 'workspace';
  isExpanded?: boolean;
  onToggleExpanded?: () => void;
  refreshNonce?: string | number;
}

type OwnedCloseTarget =
  | (SessionFileOwnerIdentity & { kind: 'tab'; path: string })
  | (SessionFileOwnerIdentity & { kind: 'panel' });

type PendingClose = OwnedCloseTarget & { requestNonce: number };

export interface ArtifactsPanelSaveResult extends SessionFileOwnerIdentity {
  ok: boolean;
  stale: boolean;
  failedPaths: string[];
}

export interface ArtifactsPanelHandle extends SessionFileOwnerIdentity {
  hasDirty: (expectedOwner: SessionFileOwnerIdentity) => boolean;
  saveDirty: (expectedOwner: SessionFileOwnerIdentity) => Promise<ArtifactsPanelSaveResult>;
}

const DIRECTORY_FOCUS_TARGET = '__directory__';

interface SessionPanelState {
  currentPath: string;
  pathHistory: string[];
  historyIndex: number;
  openTabs: FileInfo[];
  activePath: string | null;
  listScrollTops: Record<string, number>;
  lastTriggerPath: string;
  lastExternalTargetKey: string | null;
}

function createSessionPanelState(): SessionPanelState {
  return {
    currentPath: '',
    pathHistory: [''],
    historyIndex: 0,
    openTabs: [],
    activePath: null,
    listScrollTops: {},
    lastTriggerPath: '',
    lastExternalTargetKey: null,
  };
}

function applyExternalTarget(
  current: SessionPanelState,
  targetFile: FileInfo,
  targetKey: string,
): SessionPanelState {
  const normalizedTarget = normalizeTargetFile(targetFile);
  const normalizedPath = normalizePathForCompare(normalizedTarget.path);
  const parentPath = getParentPath(normalizedTarget.path);
  const existingIndex = current.openTabs.findIndex(
    (file) => normalizePathForCompare(file.path) === normalizedPath,
  );
  const openTabs = existingIndex >= 0
    ? current.openTabs.map((file, index) => (
      index === existingIndex ? mergeFileInfo(file, normalizedTarget) : file
    ))
    : [...current.openTabs, normalizedTarget];
  const pathHistory = current.currentPath === parentPath
    ? current.pathHistory
    : [...current.pathHistory.slice(0, current.historyIndex + 1), parentPath];

  return {
    ...current,
    currentPath: parentPath,
    pathHistory,
    historyIndex: pathHistory.length - 1,
    openTabs,
    activePath: normalizedPath,
    lastExternalTargetKey: targetKey,
  };
}

export const ArtifactsPanel = forwardRef<ArtifactsPanelHandle, ArtifactsPanelProps>(function ArtifactsPanel({
  sessionId,
  ownerEpoch = 0,
  isOpen,
  onClose,
  targetFile,
  targetFileNonce,
  variant = 'drawer',
  isExpanded = false,
  onToggleExpanded,
  refreshNonce,
}: ArtifactsPanelProps, ref) {
  const [isMounted, setIsMounted] = useState(isOpen);
  const [items, setItems] = useState<FileInfo[]>([]);
  const [loading, setLoading] = useState(false);
  const [loadError, setLoadError] = useState('');
  const [sessionStates, setSessionStates] = useState<Record<string, SessionPanelState>>({});
  const [dirtyPaths, setDirtyPaths] = useState<Record<string, boolean>>({});
  const [savingPaths, setSavingPaths] = useState<Record<string, boolean>>({});
  const [failedClosePaths, setFailedClosePaths] = useState<Record<string, boolean>>({});
  const [pendingClose, setPendingClose] = useState<PendingClose | null>(null);
  const [discardConfirm, setDiscardConfirm] = useState<OwnedCloseTarget | null>(null);
  const directoryRequestSeqRef = useRef(0);
  const listScrollRef = useRef<HTMLDivElement>(null);
  const directoryButtonRef = useRef<HTMLButtonElement>(null);
  const tabButtonRefsRef = useRef(new Map<string, HTMLButtonElement>());
  const pendingFocusPathRef = useRef<string | null>(null);
  const refreshNoncesRef = useRef(new Map<string, string | number | undefined>());
  const closeTabRef = useRef<(path: string, force?: boolean) => void>(() => {});
  const closeRequestSeqRef = useRef(0);
  const pendingCloseRef = useRef(pendingClose);
  const previewHandlesRef = useRef(new Map<string, FilePreviewHandle>());
  const dirtyPathsRef = useRef(dirtyPaths);
  const ownerIdentityRef = useRef<SessionFileOwnerIdentity>({ ownerSessionId: sessionId, ownerEpoch });
  const discardDialogRef = useRef<HTMLDivElement>(null);
  const discardReturnFocusRef = useRef<HTMLElement | null>(null);
  const ownerIdentity = useMemo(
    () => ({ ownerSessionId: sessionId, ownerEpoch }),
    [ownerEpoch, sessionId],
  );
  dirtyPathsRef.current = dirtyPaths;
  ownerIdentityRef.current = ownerIdentity;
  pendingCloseRef.current = pendingClose;

  const storedSessionState = sessionStates[sessionId] ?? createSessionPanelState();
  const externalTargetKey = targetFile
    ? `${targetFileNonce ?? 'initial'}:${normalizePathForCompare(targetFile.path)}`
    : null;
  // 外部文件卡片入口必须在工作台出现的首帧就投影目标文件，不能先画目录再由 effect 切换。
  const sessionState = targetFile && externalTargetKey
    && storedSessionState.lastExternalTargetKey !== externalTargetKey
    ? applyExternalTarget(storedSessionState, targetFile, externalTargetKey)
    : storedSessionState;
  const {
    activePath,
    currentPath,
    historyIndex,
    openTabs,
    pathHistory,
  } = sessionState;
  const activeFile = activePath
    ? openTabs.find((file) => normalizePathForCompare(file.path) === activePath) ?? null
    : null;

  const updateOwnedSessionState = useCallback((
    ownerSessionId: string,
    updater: (current: SessionPanelState) => SessionPanelState,
  ) => {
    setSessionStates((previous) => {
      const current = previous[ownerSessionId] ?? createSessionPanelState();
      const next = updater(current);
      return next === current ? previous : { ...previous, [ownerSessionId]: next };
    });
  }, []);

  const updateSessionState = useCallback((
    updater: (current: SessionPanelState) => SessionPanelState,
  ) => {
    updateOwnedSessionState(sessionId, updater);
  }, [sessionId, updateOwnedSessionState]);

  useEffect(() => {
    if (isOpen) setIsMounted(true);
  }, [isOpen]);

  useEffect(() => {
    directoryRequestSeqRef.current += 1;
    setItems([]);
    setLoading(false);
    setLoadError('');
  }, [sessionId]);

  useLayoutEffect(() => {
    if (!isOpen || !sessionId || !targetFile || !externalTargetKey) return;
    updateSessionState((current) => {
      if (current.lastExternalTargetKey === externalTargetKey) return current;
      return sessionState;
    });
  }, [externalTargetKey, isOpen, sessionId, sessionState, targetFile, updateSessionState]);

  const loadDir = useCallback(async (path: string) => {
    const requestSeq = ++directoryRequestSeqRef.current;
    const requestedSessionId = sessionId;
    setLoading(true);
    setLoadError('');
    try {
      const response = await apiService.getSessionFiles(
        requestedSessionId,
        path || undefined,
      );
      if (directoryRequestSeqRef.current === requestSeq) setItems(response.files);
    } catch (error) {
      console.error('Failed to load directory:', error);
      if (directoryRequestSeqRef.current === requestSeq) {
        setItems([]);
        setLoadError('无法读取此目录');
      }
    } finally {
      if (directoryRequestSeqRef.current === requestSeq) setLoading(false);
    }
  }, [sessionId]);

  useEffect(() => {
    if (isOpen && sessionId) void loadDir(currentPath);
  }, [isOpen, sessionId, currentPath, loadDir]);

  useEffect(() => {
    if (!sessionId || refreshNonce === undefined) return;
    if (!refreshNoncesRef.current.has(sessionId)) {
      refreshNoncesRef.current.set(sessionId, refreshNonce);
      return;
    }
    if (refreshNoncesRef.current.get(sessionId) === refreshNonce) return;
    refreshNoncesRef.current.set(sessionId, refreshNonce);
    if (isOpen) void loadDir(currentPath);
  }, [currentPath, isOpen, loadDir, refreshNonce, sessionId]);

  useEffect(() => {
    if (items.length === 0) return;
    updateSessionState((current) => {
      let changed = false;
      const nextTabs = current.openTabs.map((tab) => {
        const matched = items.find((item) => (
          !item.is_directory
          && normalizePathForCompare(item.path) === normalizePathForCompare(tab.path)
        ));
        if (!matched) return tab;
        const merged = mergeFileInfo(tab, matched);
        if (merged === tab) return tab;
        changed = true;
        return merged;
      });
      return changed ? { ...current, openTabs: nextTabs } : current;
    });
  }, [items, updateSessionState]);

  useLayoutEffect(() => {
    if (activePath || !listScrollRef.current) return;
    listScrollRef.current.scrollTop = sessionState.listScrollTops[currentPath] ?? 0;
    if (!sessionState.lastTriggerPath) return;
    const restoredTrigger = Array.from(
      listScrollRef.current.querySelectorAll<HTMLElement>('[data-file-path]'),
    ).find((element) => element.dataset.filePath === sessionState.lastTriggerPath);
    if (restoredTrigger) restoredTrigger.focus({ preventScroll: true });
    else directoryButtonRef.current?.focus({ preventScroll: true });
  }, [activePath, currentPath, items, sessionState.lastTriggerPath, sessionState.listScrollTops]);

  useLayoutEffect(() => {
    const pendingPath = pendingFocusPathRef.current;
    if (!pendingPath) return;
    pendingFocusPathRef.current = null;
    if (pendingPath === DIRECTORY_FOCUS_TARGET) {
      directoryButtonRef.current?.focus({ preventScroll: true });
      return;
    }
    tabButtonRefsRef.current.get(pendingPath)?.focus({ preventScroll: true });
  }, [activePath, openTabs]);

  const rememberCurrentListScroll = (current: SessionPanelState): SessionPanelState => ({
    ...current,
    listScrollTops: {
      ...current.listScrollTops,
      [current.currentPath]: listScrollRef.current?.scrollTop
        ?? current.listScrollTops[current.currentPath]
        ?? 0,
    },
  });

  const navigateTo = (subPath: string) => {
    updateSessionState((current) => {
      const remembered = rememberCurrentListScroll(current);
      const newHistory = remembered.pathHistory.slice(0, remembered.historyIndex + 1);
      if (newHistory[newHistory.length - 1] !== subPath) newHistory.push(subPath);
      return {
        ...remembered,
        currentPath: subPath,
        pathHistory: newHistory,
        historyIndex: newHistory.length - 1,
        activePath: null,
      };
    });
  };

  const goBack = () => {
    updateSessionState((current) => {
      if (current.historyIndex <= 0) return current;
      const remembered = rememberCurrentListScroll(current);
      const nextIndex = remembered.historyIndex - 1;
      return {
        ...remembered,
        historyIndex: nextIndex,
        currentPath: remembered.pathHistory[nextIndex],
        activePath: null,
      };
    });
  };

  const goForward = () => {
    updateSessionState((current) => {
      if (current.historyIndex >= current.pathHistory.length - 1) return current;
      const remembered = rememberCurrentListScroll(current);
      const nextIndex = remembered.historyIndex + 1;
      return {
        ...remembered,
        historyIndex: nextIndex,
        currentPath: remembered.pathHistory[nextIndex],
        activePath: null,
      };
    });
  };

  const goUp = () => {
    if (!currentPath) return;
    const parent = currentPath.includes('/')
      ? currentPath.substring(0, currentPath.lastIndexOf('/'))
      : '';
    navigateTo(parent);
  };

  const showDirectory = () => {
    updateSessionState((current) => ({
      ...rememberCurrentListScroll(current),
      activePath: null,
    }));
  };

  const openFile = (file: FileInfo, triggerPath = '') => {
    const normalizedPath = normalizePathForCompare(file.path);
    updateSessionState((current) => {
      const remembered = rememberCurrentListScroll(current);
      const existingIndex = remembered.openTabs.findIndex(
        (tab) => normalizePathForCompare(tab.path) === normalizedPath,
      );
      return {
        ...remembered,
        openTabs: existingIndex >= 0
          ? remembered.openTabs.map((tab, index) => (index === existingIndex ? file : tab))
          : [...remembered.openTabs, file],
        activePath: normalizedPath,
        lastTriggerPath: triggerPath || remembered.lastTriggerPath,
      };
    });
  };

  const ownerKeyPrefix = `${sessionId}:${ownerEpoch}:`;
  const dirtyKey = (path: string) => `${ownerKeyPrefix}${normalizePathForCompare(path)}`;
  const hasDirtyFiles = Object.entries(dirtyPaths).some(
    ([key, dirty]) => dirty && key.startsWith(ownerKeyPrefix),
  );
  const hasSavingFiles = Object.entries(savingPaths).some(
    ([key, saving]) => saving && key.startsWith(ownerKeyPrefix),
  );
  const hasFailedDirtyFiles = Object.entries(failedClosePaths).some(
    ([key, failed]) => failed && key.startsWith(ownerKeyPrefix) && dirtyPaths[key],
  );

  const requestDiscardConfirm = (target: OwnedCloseTarget) => {
    discardReturnFocusRef.current = document.activeElement instanceof HTMLElement
      ? document.activeElement
      : null;
    setDiscardConfirm(target);
  };

  const cancelDiscardConfirm = () => {
    setDiscardConfirm(null);
    const returnFocus = discardReturnFocusRef.current;
    discardReturnFocusRef.current = null;
    window.requestAnimationFrame(() => returnFocus?.focus({ preventScroll: true }));
  };

  const confirmDiscardClose = () => {
    const target = discardConfirm;
    if (!target || !sameOwner(target, ownerIdentityRef.current)) {
      setDiscardConfirm(null);
      return;
    }
    pendingCloseRef.current = null;
    setPendingClose(null);
    setDiscardConfirm(null);
    if (target.kind === 'tab') {
      closeTabRef.current(target.path, true);
      return;
    }
    const prefix = ownerKey(target);
    setDirtyPaths((current) => removeRecordPrefix(current, prefix));
    setSavingPaths((current) => removeRecordPrefix(current, prefix));
    setFailedClosePaths((current) => removeRecordPrefix(current, prefix));
    onClose();
  };

  const discardConfirmOwned = Boolean(discardConfirm && sameOwner(discardConfirm, ownerIdentity));

  useLayoutEffect(() => {
    if (discardConfirmOwned) discardDialogRef.current?.focus({ preventScroll: true });
  }, [discardConfirmOwned]);

  useEffect(() => {
    if (!discardConfirm || sameOwner(discardConfirm, ownerIdentity)) return;
    setDiscardConfirm(null);
    discardReturnFocusRef.current = null;
  }, [discardConfirm, ownerIdentity]);

  useImperativeHandle(ref, () => ({
    ...ownerIdentity,
    hasDirty: (expectedOwner) => {
      const currentOwner = ownerIdentityRef.current;
      if (!sameOwner(currentOwner, expectedOwner)) return false;
      const prefix = ownerKey(expectedOwner);
      const stateReportsDirty = Object.entries(dirtyPathsRef.current).some(
        ([key, dirty]) => dirty && key.startsWith(prefix),
      );
      if (stateReportsDirty) return true;
      return Array.from(previewHandlesRef.current.entries()).some(
        ([key, handle]) => key.startsWith(prefix) && handle.isDirty(expectedOwner),
      );
    },
    saveDirty: async (expectedOwner) => {
      const currentOwner = ownerIdentityRef.current;
      if (!sameOwner(currentOwner, expectedOwner)) {
        return { ...expectedOwner, ok: false, stale: true, failedPaths: [] };
      }

      const prefix = ownerKey(expectedOwner);
      const dirtyKeys = new Set(
        Object.entries(dirtyPathsRef.current)
          .filter(([key, dirty]) => dirty && key.startsWith(prefix))
          .map(([key]) => key),
      );
      for (const [key, handle] of previewHandlesRef.current.entries()) {
        if (key.startsWith(prefix) && handle.isDirty(expectedOwner)) dirtyKeys.add(key);
      }
      const results = await Promise.all(Array.from(dirtyKeys).map(async (key) => {
        const path = key.slice(prefix.length);
        const handle = previewHandlesRef.current.get(key);
        if (!handle) return { path, ok: false };
        const result = await handle.saveDirty(expectedOwner);
        return { path, ok: result.ok && !result.stale };
      }));
      const stale = !sameOwner(ownerIdentityRef.current, expectedOwner);
      const failedPaths = results.filter((result) => !result.ok).map((result) => result.path);
      return {
        ...expectedOwner,
        ok: !stale && failedPaths.length === 0,
        stale,
        failedPaths,
      };
    },
  }), [ownerIdentity]);

  const closeTab = (path: string, force = false) => {
    const normalizedPath = normalizePathForCompare(path);
    if (!force && dirtyPaths[dirtyKey(normalizedPath)] && failedClosePaths[dirtyKey(normalizedPath)]) {
      requestDiscardConfirm({ ...ownerIdentity, kind: 'tab', path: normalizedPath });
      return;
    }
    if (!force && savingPaths[dirtyKey(normalizedPath)]) {
      setPendingClose({ ...ownerIdentity, kind: 'tab', path: normalizedPath, requestNonce: ++closeRequestSeqRef.current });
      return;
    }
    if (!force && dirtyPaths[dirtyKey(normalizedPath)]) {
      setPendingClose({ ...ownerIdentity, kind: 'tab', path: normalizedPath, requestNonce: ++closeRequestSeqRef.current });
      return;
    }
    const closingIndex = openTabs.findIndex(
      (tab) => normalizePathForCompare(tab.path) === normalizedPath,
    );
    if (closingIndex < 0) return;
    const remainingTabs = openTabs.filter((_, index) => index !== closingIndex);
    const nextActivePath = activePath === normalizedPath
      ? normalizePathForCompare(
        remainingTabs[Math.min(closingIndex, remainingTabs.length - 1)]?.path || '',
      )
      : activePath;
    pendingFocusPathRef.current = nextActivePath
      || (sessionState.lastTriggerPath ? null : DIRECTORY_FOCUS_TARGET);

    updateSessionState((current) => {
      const currentClosingIndex = current.openTabs.findIndex(
        (tab) => normalizePathForCompare(tab.path) === normalizedPath,
      );
      if (currentClosingIndex < 0) return current;
      const nextTabs = current.openTabs.filter((_, index) => index !== currentClosingIndex);
      if (current.activePath !== normalizedPath) return { ...current, openTabs: nextTabs };
      const nextActive = nextTabs[Math.min(currentClosingIndex, nextTabs.length - 1)] ?? null;
      return {
        ...current,
        openTabs: nextTabs,
        activePath: nextActive ? normalizePathForCompare(nextActive.path) : null,
      };
    });
    setDirtyPaths((current) => {
      const next = { ...current };
      delete next[dirtyKey(normalizedPath)];
      return next;
    });
    setSavingPaths((current) => {
      const next = { ...current };
      delete next[dirtyKey(normalizedPath)];
      return next;
    });
    setFailedClosePaths((current) => {
      const next = { ...current };
      delete next[dirtyKey(normalizedPath)];
      return next;
    });
  };
  closeTabRef.current = closeTab;

  const requestPanelClose = () => {
    if (hasFailedDirtyFiles) {
      requestDiscardConfirm({ ...ownerIdentity, kind: 'panel' });
      return;
    }
    if (hasSavingFiles || hasDirtyFiles) {
      setPendingClose({ ...ownerIdentity, kind: 'panel', requestNonce: ++closeRequestSeqRef.current });
      return;
    }
    onClose();
  };

  const pendingCloseOwned = Boolean(pendingClose && sameOwner(pendingClose, ownerIdentity));
  const pendingCloseSaving = Boolean(pendingCloseOwned && pendingClose && (
    pendingClose.kind === 'tab'
      ? savingPaths[dirtyKey(pendingClose.path)]
      : hasSavingFiles
  ));
  const pendingCloseDirty = Boolean(pendingCloseOwned && pendingClose && (
    pendingClose.kind === 'tab'
      ? dirtyPaths[dirtyKey(pendingClose.path)]
      : hasDirtyFiles
  ));
  useEffect(() => {
    if (!pendingClose) return;
    if (!sameOwner(pendingClose, ownerIdentityRef.current)) {
      setPendingClose((current) => (current === pendingClose ? null : current));
      return;
    }
    if (pendingCloseSaving || pendingCloseDirty) return;
    const completedClose = pendingClose;
    setPendingClose(null);
    if (completedClose.kind === 'tab') {
      closeTabRef.current(completedClose.path, true);
    } else {
      onClose();
    }
  }, [onClose, pendingClose, pendingCloseDirty, pendingCloseSaving]);

  const handleFileUpdated = (updated: FileInfo, owner: SessionFileOwnerIdentity) => {
    if (!sameOwner(ownerIdentityRef.current, owner)) return;
    const updatedPath = normalizePathForCompare(updated.path);
    setFailedClosePaths((current) => {
      const key = `${ownerKey(owner)}${updatedPath}`;
      if (!current[key]) return current;
      const next = { ...current };
      delete next[key];
      return next;
    });
    setItems((current) => current.map((item) => (
      normalizePathForCompare(item.path) === updatedPath ? { ...item, ...updated } : item
    )));
    updateOwnedSessionState(owner.ownerSessionId, (current) => ({
      ...current,
      openTabs: current.openTabs.map((tab) => (
        normalizePathForCompare(tab.path) === updatedPath ? { ...tab, ...updated } : tab
      )),
    }));
  };

  const handleItemClick = (item: FileInfo, triggerPath?: string) => {
    if (item.is_directory) navigateTo(item.path);
    else openFile(item, triggerPath);
  };

  const handleDownload = async (file: FileInfo, event: MouseEvent) => {
    event.stopPropagation();
    try {
      await apiService.downloadFile(sessionId, file.path);
    } catch (error) {
      console.error('Failed to download file:', error);
    }
  };

  const shortSessionId = sessionId.length > 12
    ? `${sessionId.substring(0, 8)}...`
    : sessionId;
  const displayPath = currentPath
    ? `~/sessions/${shortSessionId}/${currentPath}`
    : `~/sessions/${shortSessionId}`;

  const headerClassName = variant === 'workspace'
    ? 'flex h-14 shrink-0 items-center border-b border-claude-border bg-white px-3'
    : 'border-b border-claude-border px-4 py-3';

  const content = (
    <div className="relative flex h-full min-h-0 flex-col bg-white">
      <div
        className={headerClassName}
        data-testid={variant === 'workspace' ? 'session-files-toolbar' : undefined}
      >
        <div className="flex w-full items-center gap-1">
          <button
            type="button"
            onClick={goBack}
            disabled={historyIndex <= 0}
            className="inline-flex h-8 w-8 items-center justify-center rounded-md text-claude-muted hover:bg-claude-hover disabled:cursor-not-allowed disabled:opacity-30"
            title="后退"
            aria-label="后退"
          >
            <ChevronLeft size={15} aria-hidden="true" />
          </button>
          <button
            type="button"
            onClick={goForward}
            disabled={historyIndex >= pathHistory.length - 1}
            className="inline-flex h-8 w-8 items-center justify-center rounded-md text-claude-muted hover:bg-claude-hover disabled:cursor-not-allowed disabled:opacity-30"
            title="前进"
            aria-label="前进"
          >
            <ChevronRight size={15} aria-hidden="true" />
          </button>
          <button
            type="button"
            onClick={goUp}
            disabled={!currentPath}
            className="inline-flex h-8 w-8 items-center justify-center rounded-md text-claude-muted hover:bg-claude-hover disabled:cursor-not-allowed disabled:opacity-30"
            title="上级目录"
            aria-label="上级目录"
          >
            <ArrowUp size={15} aria-hidden="true" />
          </button>
          <button
            ref={directoryButtonRef}
            type="button"
            onClick={showDirectory}
            className={`inline-flex h-8 w-8 items-center justify-center rounded-md transition-colors ${
              activePath ? 'text-claude-muted hover:bg-claude-hover' : 'bg-claude-hover text-claude-text'
            }`}
            title="查看目录"
            aria-label="查看目录"
            aria-pressed={!activePath}
          >
            <FolderTree size={15} aria-hidden="true" />
          </button>
          <button
            type="button"
            onClick={() => void loadDir(currentPath)}
            disabled={loading}
            className="inline-flex h-8 w-8 items-center justify-center rounded-md text-claude-muted transition-colors hover:bg-claude-hover hover:text-claude-text disabled:cursor-wait disabled:opacity-45"
            title="刷新当前目录"
            aria-label="刷新当前目录"
          >
            <RotateCcw size={14} className={loading ? 'animate-spin' : ''} aria-hidden="true" />
          </button>
          {openTabs.length > 0 ? (
            <div
              role="tablist"
              aria-label="已打开文件"
              className="ml-1 flex min-w-0 flex-1 items-center gap-1 overflow-x-auto"
            >
              {openTabs.map((tab) => {
                const tabPath = normalizePathForCompare(tab.path);
                const isActive = activePath === tabPath;
                const Icon = getFileIcon(tab);
                return (
                  <div
                    key={tabPath}
                    className={`group flex h-8 max-w-[220px] shrink-0 items-center rounded-md border ${
                      isActive
                        ? 'border-claude-border bg-white text-claude-text shadow-sm'
                        : 'border-transparent bg-claude-surface/65 text-claude-muted hover:bg-claude-hover hover:text-claude-text'
                    }`}
                  >
                    <button
                      ref={(node) => {
                        if (node) tabButtonRefsRef.current.set(tabPath, node);
                        else tabButtonRefsRef.current.delete(tabPath);
                      }}
                      type="button"
                      role="tab"
                      aria-selected={isActive}
                      title={tab.name}
                      onClick={() => openFile(tab)}
                      className="flex min-w-0 flex-1 items-center gap-1.5 py-1.5 pl-2.5 text-xs focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-claude-accent/45"
                    >
                      <Icon size={13} className={getFileIconClass(tab)} aria-hidden="true" />
                      <span className="truncate">{tab.name}</span>
                    </button>
                    <button
                      type="button"
                      onClick={() => closeTab(tab.path)}
                      className="mr-1 inline-flex h-6 w-6 items-center justify-center rounded text-claude-muted opacity-70 hover:bg-claude-hover hover:text-claude-text group-hover:opacity-100 focus-visible:opacity-100 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-claude-accent/45"
                      aria-label={`关闭 ${tab.name}`}
                      title={`关闭 ${tab.name}`}
                    >
                      <X size={12} aria-hidden="true" />
                    </button>
                  </div>
                );
              })}
            </div>
          ) : (
            <div
              className="ml-1 flex h-8 min-w-0 flex-1 items-center truncate rounded-md bg-claude-surface px-2.5 text-[11px] font-mono text-claude-muted select-all"
              title={displayPath}
            >
              {displayPath}
            </div>
          )}
          {variant === 'workspace' && onToggleExpanded && (
            <SessionFilesExpandButton expanded={isExpanded} onToggle={onToggleExpanded} />
          )}
          <button
            type="button"
            onClick={requestPanelClose}
            className="inline-flex h-8 w-8 items-center justify-center rounded-md text-claude-muted transition-colors hover:bg-claude-hover hover:text-claude-text focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-claude-accent/45"
            aria-label="收起文件"
            title="收起文件"
          >
            <X size={15} aria-hidden="true" />
          </button>
        </div>
      </div>

      <div
        className={`${activeFile ? 'hidden' : 'flex'} min-h-0 flex-1 flex-col`}
        aria-hidden={Boolean(activeFile)}
      >
          <div
            ref={listScrollRef}
            data-testid="artifacts-file-list"
            className="flex-1 space-y-1 overflow-y-auto p-3"
          >
            {loading ? (
              <div className="flex items-center justify-center py-12" aria-label="正在加载文件">
                <Loader2 className="h-6 w-6 animate-spin text-claude-muted" />
              </div>
            ) : loadError ? (
              <div className="flex h-full min-h-[300px] flex-col items-center justify-center text-center">
                <FolderOpen size={38} className="mb-3 text-claude-border" />
                <p className="mb-3 text-[13px] text-claude-muted">{loadError}</p>
                <button
                  type="button"
                  onClick={() => void loadDir(currentPath)}
                  className="inline-flex items-center gap-1.5 rounded-lg border border-claude-border px-3 py-1.5 text-xs text-claude-text hover:bg-claude-hover"
                >
                  <RotateCcw size={13} aria-hidden="true" />
                  重试
                </button>
              </div>
            ) : items.length === 0 ? (
              <div className="flex h-full min-h-[300px] flex-col items-center justify-center text-center">
                <FolderOpen size={38} className="mb-3 text-claude-border" />
                <p className="text-[13px] text-claude-muted">空目录</p>
              </div>
            ) : (
              items.map((item) => {
                const itemPath = normalizePathForCompare(item.path);
                const isOpenTab = !item.is_directory && openTabs.some(
                  (tab) => normalizePathForCompare(tab.path) === itemPath,
                );
                return (
                  <div
                    key={item.path}
                    onClick={() => handleItemClick(item, item.path)}
                    tabIndex={0}
                    role="button"
                    aria-label={`${item.is_directory ? '打开目录' : '预览文件'} ${item.name}`}
                    onKeyDown={(event) => {
                      if (event.target !== event.currentTarget) return;
                      if (event.key === 'Enter' || event.key === ' ') {
                        event.preventDefault();
                        handleItemClick(item, item.path);
                      }
                    }}
                    data-file-path={item.path}
                    className={`group flex cursor-pointer items-center justify-between rounded-xl px-3 py-2.5 transition-colors active:scale-[0.99] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-claude-accent/50 ${
                      isOpenTab ? 'bg-claude-hover/70' : 'hover:bg-claude-hover'
                    }`}
                  >
                    <div className="flex min-w-0 items-center space-x-3 overflow-hidden">
                      <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-claude-surface">
                        {item.is_directory ? (
                          <Folder size={15} className="text-claude-accent" />
                        ) : (() => {
                          const Icon = getFileIcon(item);
                          return <Icon size={15} className={getFileIconClass(item)} />;
                        })()}
                      </div>
                      <div className="min-w-0 truncate">
                        <p className="truncate text-[13px] font-medium leading-tight text-claude-text">
                          {item.name}{item.is_directory ? '/' : ''}
                        </p>
                        <p className="mt-0.5 text-[10px] text-claude-muted">
                          {item.is_directory
                            ? formatRelativeTime(item.modified)
                            : `${formatFileSize(item.size)} · ${formatRelativeTime(item.modified)}`}
                        </p>
                      </div>
                    </div>

                    {!item.is_directory && (
                      <div className="flex shrink-0 items-center opacity-0 transition-opacity group-hover:opacity-100 group-focus-within:opacity-100">
                        <button
                          type="button"
                          onClick={(event) => void handleDownload(item, event)}
                          className="rounded-lg p-1.5 text-claude-muted transition-colors hover:bg-claude-surface hover:text-claude-text"
                          title={`下载 ${item.name}`}
                          aria-label={`下载 ${item.name}`}
                        >
                          <Download size={13} aria-hidden="true" />
                        </button>
                      </div>
                    )}
                  </div>
                );
              })
            )}
          </div>

          <div className="flex items-center justify-between border-t border-claude-border px-4 py-2.5 text-[10px] text-claude-muted">
            <span>{items.length} 项</span>
            <span className="ml-2 truncate font-mono" title={displayPath}>{displayPath}</span>
          </div>
      </div>

      {openTabs.length > 0 && (
        <div
          className={`${activeFile ? 'block' : 'hidden'} min-h-0 flex-1 bg-white`}
          aria-hidden={!activeFile}
        >
          {openTabs.map((tab) => {
            const tabPath = normalizePathForCompare(tab.path);
            const isActive = activePath === tabPath;
            const previewKey = dirtyKey(tab.path);
            return (
              <div
                key={tabPath}
                className={isActive ? 'h-full min-h-0' : 'hidden'}
                aria-hidden={!isActive}
                data-preview-path={tabPath}
              >
                <FilePreview
                  ref={(handle) => {
                    if (handle) previewHandlesRef.current.set(previewKey, handle);
                    else previewHandlesRef.current.delete(previewKey);
                  }}
                  inline
                  sessionId={sessionId}
                  ownerEpoch={ownerEpoch}
                  file={tab}
                  onClose={() => closeTab(tab.path)}
                  onDirtyChange={(dirty) => {
                    const key = dirtyKey(tab.path);
                    setDirtyPaths((current) => (
                      current[key] === dirty ? current : { ...current, [key]: dirty }
                    ));
                    if (!dirty) {
                      setFailedClosePaths((current) => {
                        if (!current[key]) return current;
                        const next = { ...current };
                        delete next[key];
                        return next;
                      });
                    }
                  }}
                  onSavingChange={(saving) => {
                    const key = dirtyKey(tab.path);
                    setSavingPaths((current) => (
                      current[key] === saving ? current : { ...current, [key]: saving }
                    ));
                  }}
                  onSaveFailure={() => {
                    const key = dirtyKey(tab.path);
                    setFailedClosePaths((current) => ({ ...current, [key]: true }));
                    setPendingClose((current) => {
                      if (!current || !sameOwner(current, ownerIdentity)) return current;
                      if (current.kind === 'panel') return null;
                      return normalizePathForCompare(current.path) === tabPath ? null : current;
                    });
                  }}
                  saveRequestNonce={pendingCloseOwned && pendingClose && (
                    pendingClose.kind === 'panel'
                    || normalizePathForCompare(pendingClose.path) === tabPath
                  ) ? pendingClose.requestNonce : undefined}
                  onFileUpdated={(updated) => handleFileUpdated(updated, ownerIdentity)}
                />
              </div>
            );
          })}
        </div>
      )}
      {discardConfirmOwned && discardConfirm && (
        <div
          className="absolute inset-0 z-50 flex items-center justify-center bg-black/25 px-4"
          onClick={cancelDiscardConfirm}
        >
          <div
            ref={discardDialogRef}
            role="alertdialog"
            aria-modal="true"
            aria-label="放弃未保存修改"
            tabIndex={-1}
            onClick={(event) => event.stopPropagation()}
            onKeyDown={(event) => {
              if (event.key === 'Escape') {
                event.stopPropagation();
                cancelDiscardConfirm();
              }
            }}
            className="w-full max-w-[420px] rounded-2xl border border-claude-border bg-white p-5 shadow-2xl outline-none"
          >
            <h3 className="text-base font-semibold text-claude-text">保存失败，仍要关闭吗？</h3>
            <p className="mt-2 text-sm leading-6 text-claude-secondary">
              {discardConfirm.kind === 'tab'
                ? `“${discardConfirm.path}”的本地修改尚未保存。放弃后将关闭此标签。`
                : '仍有文件未能保存。放弃后将关闭文件面板并丢弃这些本地修改。'}
            </p>
            <div className="mt-5 flex justify-end gap-2">
              <button
                type="button"
                autoFocus
                onClick={cancelDiscardConfirm}
                className="rounded-lg border border-claude-border bg-white px-3 py-2 text-sm font-medium text-claude-text hover:bg-claude-hover"
              >
                继续编辑
              </button>
              <button
                type="button"
                onClick={confirmDiscardClose}
                className="rounded-lg bg-claude-error px-3 py-2 text-sm font-medium text-white hover:opacity-90"
              >
                放弃修改并关闭
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );

  if (variant === 'workspace') {
    if (!isOpen) return null;
    return (
      <div className="h-full min-h-0 w-full bg-white" data-testid="artifacts-panel-workspace">
        <div className="h-full min-h-0 overflow-hidden bg-white" data-testid="artifacts-panel-drawer">
          {content}
        </div>
      </div>
    );
  }

  if (!isMounted) return null;
  return (
    <>
      <div
        className={`fixed inset-0 z-20 bg-black/10 transition-opacity duration-200 ${isOpen ? 'opacity-100' : 'opacity-0'}`}
        onClick={requestPanelClose}
        onTransitionEnd={() => {
          if (!isOpen) setIsMounted(false);
        }}
      />
      <div
        className={`fixed inset-y-0 right-0 z-30 border-l border-claude-border bg-claude-bg shadow-xl transition-transform duration-300 ease-out ${
          isOpen ? 'translate-x-0' : 'translate-x-full'
        }`}
        style={{ width: 'min(920px, calc(100vw - 48px))' }}
        data-testid="artifacts-panel-drawer"
      >
        {content}
      </div>
    </>
  );
});

function normalizeTargetFile(file: FileInfo): FileInfo {
  return { ...file, path: file.path.replace(/^\/+/, '') };
}

function ownerKey(owner: SessionFileOwnerIdentity): string {
  return `${owner.ownerSessionId}:${owner.ownerEpoch}:`;
}

function sameOwner(left: SessionFileOwnerIdentity, right: SessionFileOwnerIdentity): boolean {
  return left.ownerSessionId === right.ownerSessionId && left.ownerEpoch === right.ownerEpoch;
}

function removeRecordPrefix(
  record: Record<string, boolean>,
  prefix: string,
): Record<string, boolean> {
  const entries = Object.entries(record).filter(([key]) => !key.startsWith(prefix));
  return entries.length === Object.keys(record).length ? record : Object.fromEntries(entries);
}

function mergeFileInfo(current: FileInfo, incoming: FileInfo): FileInfo {
  const normalized = normalizeTargetFile(incoming);
  const incomingVersionUnknown = normalized.size === 0 && !normalized.modified;
  const next: FileInfo = {
    ...current,
    ...normalized,
    size: incomingVersionUnknown ? current.size : normalized.size,
    modified: normalized.modified || current.modified,
    type: normalized.type || current.type,
    session_id: normalized.session_id || current.session_id,
    data_url: normalized.data_url || current.data_url,
  };
  return (
    current.name === next.name
    && current.path === next.path
    && current.session_id === next.session_id
    && current.size === next.size
    && current.modified === next.modified
    && current.type === next.type
    && current.is_directory === next.is_directory
    && current.data_url === next.data_url
  ) ? current : next;
}

function normalizePathForCompare(path: string): string {
  return path.replace(/^\/+/, '');
}

function getParentPath(path: string): string {
  const normalizedPath = normalizePathForCompare(path);
  return normalizedPath.includes('/')
    ? normalizedPath.substring(0, normalizedPath.lastIndexOf('/'))
    : '';
}

function formatFileSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function formatRelativeTime(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return '时间未知';
  return formatDistanceToNow(date, { addSuffix: true, locale: zhCN });
}
