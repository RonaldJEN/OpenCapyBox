import { useState, useEffect, useCallback, useRef, type KeyboardEvent } from 'react';
import {
  Navigate,
  RouterProvider,
  createBrowserRouter,
  useBlocker,
  useLocation,
  useNavigate,
} from 'react-router-dom';
import { Blocks, CalendarClock, Database, MessageSquare } from 'lucide-react';
import { Login } from './components/Login';
import { SessionList } from './components/SessionList';
import { AppSidebar } from './components/AppSidebar';
import { ChatV2 } from './components/ChatV2';
import type { ArtifactsPanelHandle } from './components/ArtifactsPanel';
import type { WorkspaceFilesPanelHandle } from './components/workspace/WorkspaceFilesPanel';
import AdminConsole from './components/AdminConsole';
import SettingsCenter from './components/SettingsCenter';
import SkillsPage from './components/SkillsPage';
import ConnectionsPage from './components/ConnectionsPage';
import SchedulePage from './components/SchedulePage';
import FeedbackMessage from './components/FeedbackMessage';
import { apiService } from './services/api';
import { getCronRuns, getUnreadCount } from './services/configApi';
import { startSessionDraftOutbox } from './services/sessionDraftOutbox';
import { startWorkspaceDraftOutbox } from './services/workspaceDraftOutbox';
import { ChatRuntimeProvider, useChatRuntime } from './runtime/ChatRuntimeProvider';
import { SessionStatus, type ModelInfo, type Session } from './types';
import type { WorkspaceEntry } from './types/workspace';
import { workspaceApi } from './services/workspaceApi';
import { emitWorkspaceChangeInvalidations, subscribeWorkspaceMutation } from './services/workspaceEvents';

type ConfigPanel = 'config' | null;
type PrimarySurface = 'chat' | 'schedule' | 'skills' | 'connections';
type NavigationTarget =
  | { kind: 'surface'; surface: PrimarySurface }
  | { kind: 'panel'; panel: ConfigPanel };
type DirtySource = 'settings' | 'connections';
interface PendingNavigation {
  target: NavigationTarget;
  source: 'settings';
  beforeApply?: () => void;
}
interface PendingSurfaceAction {
  surface: PrimarySurface;
  beforeApply: () => void;
}
type SessionScrollTarget = {
  sessionId: string;
  roundId: string;
  nonce: number;
};

const RUNNING_SESSIONS_RECONCILE_INTERVAL_MS = 5000;
const WORKSPACE_CRON_INVALIDATION_INTERVAL_MS = 15_000;
const WORKSPACE_CRON_INVALIDATION_PAGE_SIZE = 10;

function isWorkspacePath(pathname: string): boolean {
  return pathname.replace(/\/+$/, '') === '/workspace';
}

function primarySurfaceForPath(pathname: string): PrimarySurface {
  const normalizedPath = pathname.length > 1 ? pathname.replace(/\/+$/, '') : pathname;
  if (normalizedPath === '/schedule') return 'schedule';
  if (normalizedPath === '/skills') return 'skills';
  if (normalizedPath === '/connections') return 'connections';
  return 'chat';
}

function pathForPrimarySurface(surface: PrimarySurface): string {
  if (surface === 'schedule') return '/schedule';
  if (surface === 'skills') return '/skills';
  if (surface === 'connections') return '/connections';
  return '/';
}

// 主页面组件
function HomePage() {
  const [refreshTrigger, setRefreshTrigger] = useState(0);

  const handleTitleUpdated = useCallback(() => {
    setRefreshTrigger((prev) => prev + 1);
  }, []);

  return (
    <ChatRuntimeProvider onTitleUpdated={handleTitleUpdated}>
      <HomePageContent refreshTrigger={refreshTrigger} />
    </ChatRuntimeProvider>
  );
}

interface HomePageContentProps {
  refreshTrigger: number;
}

function HomePageContent({ refreshTrigger }: HomePageContentProps) {
  const location = useLocation();
  const navigate = useNavigate();
  const activeSurface = primarySurfaceForPath(location.pathname);
  const workspaceRouteActive = isWorkspacePath(location.pathname);
  const workspaceRouteEntryId = workspaceRouteActive
    ? new URLSearchParams(location.search).get('entry')
    : null;
  const {
    getActiveSlotSessionIds,
    getExecutingSessionIds,
    syncRunningSessions,
  } = useChatRuntime();
  const [currentSessionId, setCurrentSessionId] = useState<string>('');
  const [isSidebarCollapsed, setIsSidebarCollapsed] = useState(false);
  const [mobileSidebarOpen, setMobileSidebarOpen] = useState(false);
  const [sessionFilesOwnsBoundary, setSessionFilesOwnsBoundary] = useState(false);
  const [selectedModelId, setSelectedModelId] = useState<string>('');
  const [availableModels, setAvailableModels] = useState<ModelInfo[]>([]);
  const [optimisticSession, setOptimisticSession] = useState<Session | null>(null);
  const [activePanel, setActivePanel] = useState<ConfigPanel>(null);
  const [panelMounted, setPanelMounted] = useState(false);
  const [settingsHasUnsavedChanges, setSettingsHasUnsavedChanges] = useState(false);
  const [connectionsHaveUnsavedChanges, setConnectionsHaveUnsavedChanges] = useState(false);
  const [connectionsResetKey, setConnectionsResetKey] = useState(0);
  const [visitedPrimarySurfaces, setVisitedPrimarySurfaces] = useState(() => new Set<PrimarySurface>([activeSurface]));
  const [pendingNavigation, setPendingNavigation] = useState<PendingNavigation | null>(null);
  const [permissionsRefreshToken, setPermissionsRefreshToken] = useState(0);
  const [cronUnreadCount, setCronUnreadCount] = useState(0);
  const [sessionScrollTarget, setSessionScrollTarget] = useState<SessionScrollTarget | null>(null);
  const [sessionSwitchSaveError, setSessionSwitchSaveError] = useState('');
  const [sidebarMode, setSidebarMode] = useState<'sessions' | 'workspace'>(() => (
    workspaceRouteActive ? 'workspace' : 'sessions'
  ));
  const [workspaceFileTarget, setWorkspaceFileTarget] = useState<WorkspaceEntry | null>(null);
  const [workspaceDirectoryTargetId, setWorkspaceDirectoryTargetId] = useState<string | null>(null);
  const [workspaceFilesMounted, setWorkspaceFilesMounted] = useState(false);
  // 首帧仍由路由初始化 sidebarMode；进入页面后左右标签只切换浏览列表，
  // 不能因为查看会话列表就关闭右侧工作区或丢掉 entry 深链。
  const effectiveSidebarMode = sidebarMode;
  const shouldBlockConnectionsNavigation = useCallback(({
    currentLocation,
    nextLocation,
  }: {
    currentLocation: { pathname: string };
    nextLocation: { pathname: string };
  }) => (
    connectionsHaveUnsavedChanges
    && primarySurfaceForPath(currentLocation.pathname) === 'connections'
    && primarySurfaceForPath(nextLocation.pathname) !== 'connections'
  ), [connectionsHaveUnsavedChanges]);
  const connectionsNavigationBlocker = useBlocker(shouldBlockConnectionsNavigation);
  const sessionScrollNonceRef = useRef(0);
  const currentSessionIdRef = useRef(currentSessionId);
  const sessionFilesHandleRef = useRef<ArtifactsPanelHandle>(null);
  const workspaceFilesHandleRef = useRef<WorkspaceFilesPanelHandle | null>(null);
  const captureWorkspaceFilesHandle = useCallback((handle: WorkspaceFilesPanelHandle | null) => {
    workspaceFilesHandleRef.current = handle;
    setWorkspaceFilesMounted((mounted) => mounted === Boolean(handle) ? mounted : Boolean(handle));
  }, []);
  const workspaceOpenRequestEpochRef = useRef(0);
  // Last URL whose owner switch actually completed, so a rejected deep link
  // (address bar edit, back/forward) can be handed back to the browser.
  const committedUrlRef = useRef('/');
  const initialRunningSessionsHandledRef = useRef(false);
  const pendingSurfaceActionRef = useRef<PendingSurfaceAction | null>(null);
  const mainContentRef = useRef<HTMLDivElement>(null);
  const primaryContentRef = useRef<HTMLDivElement>(null);
  const settingsDialogRef = useRef<HTMLDivElement>(null);
  const unsavedConfirmDialogRef = useRef<HTMLDivElement>(null);
  const confirmReturnFocusRef = useRef<HTMLElement | null>(null);
  const mobileSidebarReturnFocusRef = useRef<HTMLElement | null>(null);
  currentSessionIdRef.current = currentSessionId;
  const executingSessionIds = getExecutingSessionIds();
  const activeSlotSessionIds = getActiveSlotSessionIds();
  const effectiveSidebarCollapsed = isSidebarCollapsed;
  const workspaceCronWatchActive = Boolean(workspaceFileTarget) || workspaceFilesMounted;
  const workspaceTargetResolving = Boolean(
    workspaceRouteEntryId
    && workspaceFileTarget?.entry_id !== workspaceRouteEntryId,
  );
  const unsavedConfirmSource: DirtySource | null = pendingNavigation?.source
    ?? (connectionsNavigationBlocker.state === 'blocked' ? 'connections' : null);

  useEffect(() => {
    void startSessionDraftOutbox();
    void startWorkspaceDraftOutbox();
  }, []);

  useEffect(() => {
    setVisitedPrimarySurfaces((visited) => {
      if (visited.has(activeSurface)) return visited;
      const next = new Set(visited);
      next.add(activeSurface);
      return next;
    });
  }, [activeSurface]);

  useEffect(() => {
    const pendingAction = pendingSurfaceActionRef.current;
    if (!pendingAction || pendingAction.surface !== activeSurface) return;
    pendingSurfaceActionRef.current = null;
    pendingAction.beforeApply();
  }, [activeSurface]);

  useEffect(() => {
    if (!connectionsHaveUnsavedChanges) return;
    const handleBeforeUnload = (event: BeforeUnloadEvent) => {
      event.preventDefault();
      event.returnValue = '';
    };
    window.addEventListener('beforeunload', handleBeforeUnload);
    return () => window.removeEventListener('beforeunload', handleBeforeUnload);
  }, [connectionsHaveUnsavedChanges]);

  useEffect(() => {
    if (!mobileSidebarOpen) return undefined;
    mobileSidebarReturnFocusRef.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const previousOverflow = document.body.style.overflow;
    const primaryContent = primaryContentRef.current;
    const primaryContentWasInert = primaryContent?.hasAttribute('inert') ?? false;
    document.body.style.overflow = 'hidden';
    primaryContent?.setAttribute('inert', '');
    const frame = requestAnimationFrame(() => document.querySelector<HTMLButtonElement>('[aria-label="关闭侧栏"]')?.focus());
    const handleEscape = (event: globalThis.KeyboardEvent) => {
      if (event.key === 'Escape') setMobileSidebarOpen(false);
    };
    document.addEventListener('keydown', handleEscape);
    return () => {
      cancelAnimationFrame(frame);
      document.removeEventListener('keydown', handleEscape);
      document.body.style.overflow = previousOverflow;
      if (!primaryContentWasInert) primaryContent?.removeAttribute('inert');
      mobileSidebarReturnFocusRef.current?.focus();
    };
  }, [mobileSidebarOpen]);

  const applyActivePanel = useCallback((nextPanel: ConfigPanel) => {
    if (activePanel === 'config' && nextPanel !== 'config') {
      setSettingsHasUnsavedChanges(false);
    }
    setActivePanel(nextPanel);
  }, [activePanel]);

  const applyNavigation = useCallback((target: NavigationTarget, beforeApply?: () => void) => {
    if (target.kind === 'panel') {
      beforeApply?.();
      applyActivePanel(target.panel);
      return;
    }

    if (activePanel === 'config') setSettingsHasUnsavedChanges(false);
    setActivePanel(null);
    if (target.surface === activeSurface) {
      beforeApply?.();
      return;
    }
    pendingSurfaceActionRef.current = beforeApply
      ? { surface: target.surface, beforeApply }
      : null;
    navigate(pathForPrimarySurface(target.surface));
  }, [activePanel, activeSurface, applyActivePanel, navigate]);

  const requestNavigation = useCallback((target: NavigationTarget, beforeApply?: () => void) => {
    const changesSurface = target.kind === 'surface'
      && (target.surface !== activeSurface || activePanel !== null);
    const changesPanel = target.kind === 'panel' && target.panel !== activePanel;
    if (!changesSurface && !changesPanel) {
      beforeApply?.();
      return;
    }

    if (activePanel === 'config' && settingsHasUnsavedChanges) {
      confirmReturnFocusRef.current = document.activeElement instanceof HTMLElement
        ? document.activeElement
        : null;
      setPendingNavigation({ target, source: 'settings', beforeApply });
      return;
    }

    if (
      target.kind === 'surface'
      && target.surface !== activeSurface
      && activeSurface === 'connections'
      && connectionsHaveUnsavedChanges
    ) {
      confirmReturnFocusRef.current = document.activeElement instanceof HTMLElement
        ? document.activeElement
        : null;
    }

    applyNavigation(target, beforeApply);
  }, [
    activePanel,
    activeSurface,
    applyNavigation,
    connectionsHaveUnsavedChanges,
    settingsHasUnsavedChanges,
  ]);

  const requestActivePanel = useCallback((nextPanel: ConfigPanel) => {
    requestNavigation({ kind: 'panel', panel: nextPanel });
  }, [requestNavigation]);

  const requestPrimarySurface = useCallback((surface: PrimarySurface, beforeApply?: () => void) => {
    requestNavigation({ kind: 'surface', surface }, beforeApply);
  }, [requestNavigation]);

  const requestCloseConfigPanel = useCallback(() => {
    requestActivePanel(null);
  }, [requestActivePanel]);

  const toggleSettingsPanel = useCallback(() => {
    requestActivePanel(activePanel === 'config' ? null : 'config');
  }, [activePanel, requestActivePanel]);

  // activePanel 变化时控制 mount
  useEffect(() => {
    if (activePanel) {
      setPanelMounted(true);
    }
  }, [activePanel]);

  useEffect(() => {
    const mainContent = mainContentRef.current;
    if (!mainContent) return;

    if (activePanel) {
      mainContent.setAttribute('inert', '');
      mainContent.setAttribute('aria-hidden', 'true');
    } else {
      mainContent.removeAttribute('inert');
      mainContent.removeAttribute('aria-hidden');
    }

    return () => {
      mainContent.removeAttribute('inert');
      mainContent.removeAttribute('aria-hidden');
    };
  }, [activePanel]);

  useEffect(() => {
    if (activePanel === 'config' && panelMounted) {
      settingsDialogRef.current?.focus();
    }
  }, [activePanel, panelMounted]);

  useEffect(() => {
    if (!unsavedConfirmSource) return;
    if (
      unsavedConfirmSource === 'connections'
      && !confirmReturnFocusRef.current
      && document.activeElement instanceof HTMLElement
    ) {
      confirmReturnFocusRef.current = document.activeElement;
    }
    unsavedConfirmDialogRef.current?.focus();
  }, [unsavedConfirmSource]);

  const cancelUnsavedConfirm = useCallback(() => {
    if (pendingNavigation) {
      setPendingNavigation(null);
      window.requestAnimationFrame(() => settingsDialogRef.current?.focus());
      return;
    }

    if (connectionsNavigationBlocker.state !== 'blocked') return;
    pendingSurfaceActionRef.current = null;
    connectionsNavigationBlocker.reset();
    window.requestAnimationFrame(() => {
      confirmReturnFocusRef.current?.focus();
      confirmReturnFocusRef.current = null;
    });
  }, [connectionsNavigationBlocker, pendingNavigation]);

  const discardUnsavedChanges = useCallback(() => {
    if (pendingNavigation) {
      const { target, beforeApply } = pendingNavigation;
      setPendingNavigation(null);
      setSettingsHasUnsavedChanges(false);
      applyNavigation(target, beforeApply);
      return;
    }

    if (connectionsNavigationBlocker.state !== 'blocked') return;
    setConnectionsHaveUnsavedChanges(false);
    setConnectionsResetKey((key) => key + 1);
    confirmReturnFocusRef.current = null;
    connectionsNavigationBlocker.proceed();
  }, [applyNavigation, connectionsNavigationBlocker, pendingNavigation]);

  const handleSettingsDialogKeyDown = useCallback((event: KeyboardEvent<HTMLDivElement>) => {
    if (activePanel !== 'config') return;

    if (event.key === 'Escape') {
      event.stopPropagation();
      requestCloseConfigPanel();
      return;
    }

    if (event.key !== 'Tab') return;

    const dialog = settingsDialogRef.current;
    if (!dialog) return;

    const focusable = Array.from(
      dialog.querySelectorAll<HTMLElement>(
        'a[href], button:not([disabled]), textarea:not([disabled]), input:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])',
      ),
    ).filter((element) => element.getAttribute('aria-hidden') !== 'true');

    if (focusable.length === 0) {
      event.preventDefault();
      dialog.focus();
      return;
    }

    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    const activeElement = document.activeElement;

    if (!activeElement || activeElement === dialog || !dialog.contains(activeElement)) {
      event.preventDefault();
      (event.shiftKey ? last : first).focus();
      return;
    }

    if (event.shiftKey && activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  }, [activePanel, requestCloseConfigPanel]);

  const handleUnsavedConfirmKeyDown = useCallback((event: KeyboardEvent<HTMLDivElement>) => {
    if (event.key === 'Escape') {
      event.stopPropagation();
      cancelUnsavedConfirm();
      return;
    }

    if (event.key === 'Enter' && event.target === event.currentTarget) {
      event.preventDefault();
      discardUnsavedChanges();
      return;
    }

    if (event.key !== 'Tab') return;

    const dialog = unsavedConfirmDialogRef.current;
    if (!dialog) return;

    const focusable = Array.from(
      dialog.querySelectorAll<HTMLElement>('button:not([disabled]), [tabindex]:not([tabindex="-1"])'),
    );

    if (focusable.length === 0) {
      event.preventDefault();
      dialog.focus();
      return;
    }

    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    const activeElement = document.activeElement;

    if (!activeElement || activeElement === dialog || !dialog.contains(activeElement)) {
      event.preventDefault();
      (event.shiftKey ? last : first).focus();
      return;
    }

    if (event.shiftKey && activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  }, [cancelUnsavedConfirm, discardUnsavedChanges]);

  // 統一加載模型列表（只請求一次）
  useEffect(() => {
    apiService.getModels().then((res) => {
      const models = Array.isArray(res.models) ? res.models : [];
      const defaultModelId = typeof res.default_model === 'string'
        ? res.default_model
        : models[0]?.id || '';
      setAvailableModels(models);
      setSelectedModelId((current) => current || defaultModelId);
    }).catch((err) => {
      console.error('Failed to load models:', err);
    });
  }, []);

  // 加载 Cron 未读计数 + 60s 轮询
  useEffect(() => {
    const fetchUnread = () => {
      getUnreadCount().then((r) => setCronUnreadCount(r.count)).catch(() => {});
    };
    fetchUnread();
    const timer = setInterval(fetchUnread, 60000);
    return () => clearInterval(timer);
  }, []);

  useEffect(() => {
    if (!workspaceCronWatchActive) return undefined;
    let disposed = false;
    let inFlight = false;
    let failureReported = false;

    const pollWorkspaceChanges = async () => {
      if (disposed || inFlight || document.visibilityState === 'hidden') return;
      inFlight = true;
      try {
        const response = await getCronRuns(undefined, WORKSPACE_CRON_INVALIDATION_PAGE_SIZE, 0);
        if (disposed) return;
        emitWorkspaceChangeInvalidations(response.runs.flatMap((run) => run.workspace_changes || []));
        failureReported = false;
      } catch (error) {
        if (!disposed && !failureReported) {
          failureReported = true;
          console.error('Failed to poll Cron workspace changes:', error);
        }
      } finally {
        inFlight = false;
      }
    };

    const handleVisibilityChange = () => {
      if (document.visibilityState === 'visible') void pollWorkspaceChanges();
    };
    void pollWorkspaceChanges();
    const timer = window.setInterval(
      () => { void pollWorkspaceChanges(); },
      WORKSPACE_CRON_INVALIDATION_INTERVAL_MS,
    );
    document.addEventListener('visibilitychange', handleVisibilityChange);
    return () => {
      disposed = true;
      window.clearInterval(timer);
      document.removeEventListener('visibilitychange', handleVisibilityChange);
    };
  }, [workspaceCronWatchActive]);

  const applySessionSelection = useCallback((sessionId: string, target?: { roundId: string }) => {
    currentSessionIdRef.current = sessionId;
    setCurrentSessionId(sessionId);
    if (target?.roundId) {
      setSessionScrollTarget({
        sessionId,
        roundId: target.roundId,
        nonce: ++sessionScrollNonceRef.current,
      });
    } else {
      setSessionScrollTarget(null);
    }
  }, []);

  const saveThenSelectSession = useCallback((
    sessionId: string,
    target?: { roundId: string },
  ) => {
    const ownerSessionId = currentSessionIdRef.current;
    setSessionSwitchSaveError('');

    if (ownerSessionId && ownerSessionId !== sessionId) {
      const filesHandle = sessionFilesHandleRef.current;
      if (filesHandle?.ownerSessionId === ownerSessionId) {
        const expectedOwner = {
          ownerSessionId,
          ownerEpoch: filesHandle.ownerEpoch,
        };
        if (filesHandle.hasDirty(expectedOwner)) {
          // saveDirty 在第一个网络等待前已把编辑器内容交给应用级草稿队列。
          // 切换不再等待服务器；失败的远端同步由草稿队列静默重试。
          void filesHandle.saveDirty(expectedOwner).catch((error) => {
            console.error('Failed to sync Session drafts in background:', error);
          });
        }
      }
    }
    applySessionSelection(sessionId, target);
  }, [applySessionSelection]);

  const flushCurrentSessionFiles = useCallback((): void => {
    const ownerSessionId = currentSessionIdRef.current;
    if (!ownerSessionId) return;
    const filesHandle = sessionFilesHandleRef.current;
    if (!filesHandle || filesHandle.ownerSessionId !== ownerSessionId) return;
    const expectedOwner = { ownerSessionId, ownerEpoch: filesHandle.ownerEpoch };
    if (!filesHandle.hasDirty(expectedOwner)) return;
    void filesHandle.saveDirty(expectedOwner).catch((error) => {
      console.error('Failed to sync Session drafts in background:', error);
    });
  }, []);

  const openWorkspaceEntry = useCallback(async (
    entry: WorkspaceEntry,
    options?: { replace?: boolean; preserveSidebarMode?: boolean },
  ) => {
    const requestEpoch = ++workspaceOpenRequestEpochRef.current;
    setSessionSwitchSaveError('');
    flushCurrentSessionFiles();
    if (requestEpoch !== workspaceOpenRequestEpochRef.current) return;
    if (!options?.preserveSidebarMode) setSidebarMode('workspace');
    // 同一文件标签可能已在右侧本地关闭，但 App 仍持有上一次 target。
    // 每次用户点击都投影成新的目标事件，不能被 React 的同引用去重吞掉。
    setWorkspaceDirectoryTargetId(null);
    setWorkspaceFileTarget(entry.kind === 'file' ? { ...entry } : null);
    const params = new URLSearchParams({ entry: entry.entry_id });
    navigate(`/workspace?${params.toString()}`, { replace: options?.replace });
  }, [flushCurrentSessionFiles, navigate]);

  const handleWorkspaceTabSelect = useCallback((entry: WorkspaceEntry, options?: { replace?: boolean }) => {
    void openWorkspaceEntry(entry, { ...options, preserveSidebarMode: true });
  }, [openWorkspaceEntry]);

  const requestWorkspaceEntry = useCallback((entry: WorkspaceEntry) => {
    requestPrimarySurface('chat', () => { void openWorkspaceEntry(entry); });
  }, [openWorkspaceEntry, requestPrimarySurface]);

  useEffect(() => {
    const handleWorkspaceNavigation = (event: Event) => {
      const detail = (event as CustomEvent<{ entryId?: string }>).detail;
      setSidebarMode('workspace');
      if (!detail?.entryId) {
        requestPrimarySurface('chat');
        return;
      }
      const requestEpoch = ++workspaceOpenRequestEpochRef.current;
      void workspaceApi.getEntry(detail.entryId).then((entry) => {
        if (requestEpoch !== workspaceOpenRequestEpochRef.current) return;
        if (entry.kind === 'directory') {
          setWorkspaceFileTarget(null);
          setWorkspaceDirectoryTargetId(entry.entry_id);
          requestPrimarySurface('chat');
          return;
        }
        requestWorkspaceEntry(entry);
      }).catch((error) => {
        console.error('Failed to open workspace entry:', error);
        setSessionSwitchSaveError('无法打开工作区文件，请刷新工作区后重试。');
      });
    };
    window.addEventListener('workspace:navigate', handleWorkspaceNavigation);
    return () => window.removeEventListener('workspace:navigate', handleWorkspaceNavigation);
  }, [requestPrimarySurface, requestWorkspaceEntry]);

  useEffect(() => {
    const currentUrl = `${location.pathname}${location.search}`;
    if (!workspaceRouteActive) {
      committedUrlRef.current = currentUrl;
      return;
    }
    setSidebarMode('workspace');
    const entryId = new URLSearchParams(location.search).get('entry');
    if (!entryId) {
      committedUrlRef.current = '/workspace';
      if (currentUrl !== '/workspace') navigate('/workspace', { replace: true });
      return;
    }
    const canonicalUrl = `/workspace?${new URLSearchParams({ entry: entryId }).toString()}`;
    if (workspaceFileTarget?.entry_id === entryId) {
      committedUrlRef.current = canonicalUrl;
      if (currentUrl !== canonicalUrl) navigate(canonicalUrl, { replace: true });
      return;
    }
    const requestEpoch = ++workspaceOpenRequestEpochRef.current;
    void (async () => {
      setSessionSwitchSaveError('');
      flushCurrentSessionFiles();
      if (requestEpoch !== workspaceOpenRequestEpochRef.current) return;
      try {
        const entry = await workspaceApi.getEntry(entryId);
        if (requestEpoch !== workspaceOpenRequestEpochRef.current) return;
        if (entry.kind !== 'file' || entry.status !== 'active') {
          throw new Error('Workspace deep link does not resolve to an active file');
        }
        setWorkspaceFileTarget(entry);
        committedUrlRef.current = canonicalUrl;
        if (currentUrl !== canonicalUrl) navigate(canonicalUrl, { replace: true });
      } catch (error) {
        if (requestEpoch !== workspaceOpenRequestEpochRef.current) return;
        console.error('Failed to resolve workspace deep link:', error);
        setSessionSwitchSaveError('无法解析工作区深链，请从左侧工作区重新选择文件。');
        const committedUrl = committedUrlRef.current;
        const committedLocation = new URL(committedUrl, window.location.origin);
        const committedEntryId = isWorkspacePath(committedLocation.pathname)
          ? committedLocation.searchParams.get('entry')
          : null;
        const fallbackUrl = workspaceFileTarget?.entry_id
          && committedEntryId === workspaceFileTarget.entry_id
          ? `${committedLocation.pathname}${committedLocation.search}`
          : '/workspace';
        if (currentUrl !== fallbackUrl) navigate(fallbackUrl, { replace: true });
      }
    })();
  }, [
    flushCurrentSessionFiles,
    location.pathname,
    location.search,
    navigate,
    workspaceFileTarget?.entry_id,
    workspaceRouteActive,
  ]);

  const handleWorkspaceFilesClose = useCallback(() => {
    ++workspaceOpenRequestEpochRef.current;
    setWorkspaceFileTarget(null);
    if (
      workspaceRouteActive
      && new URLSearchParams(location.search).has('entry')
    ) {
      navigate('/workspace', { replace: true });
    }
  }, [location.search, navigate, workspaceRouteActive]);

  useEffect(() => subscribeWorkspaceMutation((detail) => {
    const affectedEntryIds = [...(detail.affectedEntryIds
      || (detail.tombstone && detail.entryId ? [detail.entryId] : []))];
    const activeEntryId = workspaceFileTarget?.entry_id;
    if (!activeEntryId || !affectedEntryIds.includes(activeEntryId)) return;
    handleWorkspaceFilesClose();
  }), [handleWorkspaceFilesClose, workspaceFileTarget?.entry_id]);

  const flushWorkspaceFiles = useCallback((): void => {
    const handle = workspaceFilesHandleRef.current;
    if (!handle?.hasDirty()) return;
    void handle.saveDirty().catch((error) => {
      console.error('Failed to sync workspace files in background:', error);
    });
  }, []);

  const runAfterWorkspaceFlush = useCallback((action: () => void) => {
    // 先触发编辑器内容抓取，再立即完成用户选择；远端发布在后台收尾。
    flushWorkspaceFiles();
    action();
  }, [flushWorkspaceFiles]);

  const handleSessionSelect = useCallback((sessionId: string, target?: { roundId: string }) => {
    requestPrimarySurface('chat', () => {
      runAfterWorkspaceFlush(() => {
        setSidebarMode('sessions');
        setWorkspaceFileTarget(null);
        if (workspaceRouteActive) navigate('/');
        void saveThenSelectSession(sessionId, target);
      });
    });
  }, [navigate, requestPrimarySurface, runAfterWorkspaceFlush, saveThenSelectSession, workspaceRouteActive]);

  const handleNewChat = useCallback(() => {
    requestPrimarySurface('chat', () => {
      runAfterWorkspaceFlush(() => {
        setSidebarMode('sessions');
        setWorkspaceFileTarget(null);
        if (workspaceRouteActive) navigate('/');
        void saveThenSelectSession('');
      });
    });
  }, [navigate, requestPrimarySurface, runAfterWorkspaceFlush, saveThenSelectSession, workspaceRouteActive]);

  const handleSidebarModeChange = useCallback((mode: 'sessions' | 'workspace') => {
    if (mode === 'workspace') {
      requestPrimarySurface('chat', () => {
        setSidebarMode('workspace');
        // 工作区模式必须写入 URL，浏览器刷新才能恢复同一投影。已有 entry
        // 深链保持原 URL，不能被无参数 /workspace 覆盖。
        if (!workspaceRouteActive) navigate('/workspace');
      });
      return;
    }
    if (workspaceRouteActive) {
      setSidebarMode('sessions');
      return;
    }
    requestPrimarySurface('chat', () => setSidebarMode('sessions'));
  }, [navigate, requestPrimarySurface, workspaceRouteActive]);

  const reconcileRunningSessions = useCallback(async () => {
    try {
      const result = await apiService.getRunningSessions();
      const sessionIds = result.running_sessions.map((item) => item.session_id);
      syncRunningSessions(result.running_sessions);

      if (!initialRunningSessionsHandledRef.current) {
        initialRunningSessionsHandledRef.current = true;
        if (!currentSessionIdRef.current && sessionIds.length > 0) {
          console.log(`🔄 检测到运行中的会话: ${sessionIds.join(', ')}`);
          currentSessionIdRef.current = sessionIds[0];
          setCurrentSessionId(sessionIds[0]);
          setSessionScrollTarget(null);
        }
      }
    } catch (error) {
      console.error('Failed to reconcile running sessions:', error);
    }
  }, [syncRunningSessions]);

  useEffect(() => {
    void reconcileRunningSessions();
    const timer = setInterval(() => {
      void reconcileRunningSessions();
    }, RUNNING_SESSIONS_RECONCILE_INTERVAL_MS);

    return () => clearInterval(timer);
  }, [reconcileRunningSessions]);

  // 🆕 从 ChatV2 欢迎页触发创建会话（输入即创建）
  const handleCreateSessionForChat = useCallback(async (modelId?: string): Promise<string> => {
    const response = await apiService.createSession(modelId);
    const now = new Date().toISOString();
    setOptimisticSession({
      id: response.session_id,
      user_id: apiService.getUserId() || 'user',
      status: SessionStatus.ACTIVE,
      created_at: now,
      updated_at: now,
      title: '新会话',
      model_id: response.model_id || modelId,
    });
    return response.session_id;
  }, []);

  const handleSessionCreatedForChat = useCallback((sessionId: string) => {
    // The user may have selected another session while creation was in flight.
    // Only activate the created session while the welcome composer is still active.
    if (currentSessionIdRef.current) return;
    currentSessionIdRef.current = sessionId;
    setCurrentSessionId(sessionId);
    setSessionScrollTarget(null);
  }, []);

  return (
    <div className="flex h-screen overflow-hidden">
      <div ref={mainContentRef} data-testid="app-main-content" className="flex min-w-0 flex-1">
        <AppSidebar
          collapsed={effectiveSidebarCollapsed}
          boundaryClaimed={activeSurface === 'chat' && sessionFilesOwnsBoundary}
          mobileOpen={mobileSidebarOpen}
          userId={apiService.getUserId() || 'user'}
          onCollapsedChange={setIsSidebarCollapsed}
        >
          <SessionList
            mobileSheet={mobileSidebarOpen}
            onCloseMobileSheet={() => setMobileSidebarOpen(false)}
            currentSessionId={currentSessionId}
            onSessionSelect={(sessionId, target) => {
              setMobileSidebarOpen(false);
              handleSessionSelect(sessionId, target);
            }}
            refreshTrigger={refreshTrigger}
            optimisticSession={optimisticSession}
            executingSessionIds={executingSessionIds}
            isCollapsed={effectiveSidebarCollapsed}
            onModelChange={setSelectedModelId}
            onNewChat={() => { setMobileSidebarOpen(false); handleNewChat(); }}
            cronUnreadCount={cronUnreadCount}
            onOpenConfig={() => { setMobileSidebarOpen(false); toggleSettingsPanel(); }}
            onOpenCron={() => { setMobileSidebarOpen(false); requestPrimarySurface('schedule'); }}
            activePrimarySurface={activeSurface}
            onOpenSkills={() => { setMobileSidebarOpen(false); requestPrimarySurface('skills'); }}
            onOpenConnections={() => { setMobileSidebarOpen(false); requestPrimarySurface('connections'); }}
            sidebarMode={effectiveSidebarMode}
            onSidebarModeChange={handleSidebarModeChange}
            activeWorkspaceEntryId={workspaceFileTarget?.entry_id || workspaceDirectoryTargetId}
            onOpenWorkspaceEntry={(entry) => {
              setMobileSidebarOpen(false);
              requestWorkspaceEntry(entry);
            }}
          />
        </AppSidebar>
        <div ref={primaryContentRef} aria-hidden={mobileSidebarOpen || undefined} className="relative flex min-w-0 flex-1 overflow-hidden">
          {!activePanel && !sessionFilesOwnsBoundary && (
            <nav
              aria-label="移动端主导航"
              className="fixed left-3 top-2 z-30 flex items-center gap-1 rounded-xl border border-[#e8e3d9] bg-white/95 p-1 shadow-[0_6px_20px_rgba(30,26,20,0.10)] backdrop-blur md:hidden"
            >
              {([
                { id: 'chat' as const, label: '对话', icon: <MessageSquare size={15} /> },
                { id: 'schedule' as const, label: '日程', icon: <CalendarClock size={15} /> },
                { id: 'skills' as const, label: 'Skills', icon: <Blocks size={15} /> },
                { id: 'connections' as const, label: '数据', icon: <Database size={15} /> },
              ]).map((item) => (
                <button
                  key={item.id}
                  type="button"
                  aria-current={activeSurface === item.id ? 'page' : undefined}
                  onClick={() => {
                    if (item.id === 'chat') {
                      requestPrimarySurface('chat');
                      setMobileSidebarOpen(true);
                    } else {
                      setMobileSidebarOpen(false);
                      requestPrimarySurface(item.id);
                    }
                  }}
                  className={`flex h-8 items-center gap-1.5 rounded-lg px-2 text-[12px] font-semibold transition focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[#b8814a]/30 ${
                    activeSurface === item.id
                      ? 'bg-[#f5ece2] text-[#8a5a2f]'
                      : 'text-[#6f6960] hover:bg-[#f6f2ea]'
                  }`}
                >
                  {item.icon}
                  <span>{item.label}</span>
                </button>
              ))}
            </nav>
          )}

          <div
            data-testid="chat-primary-surface"
            aria-hidden={activeSurface !== 'chat'}
            className={activeSurface === 'chat' ? 'flex min-w-0 flex-1' : 'hidden'}
          >
            <ChatV2
              sessionId={currentSessionId}
              selectedModelId={selectedModelId}
              onModelChange={setSelectedModelId}
              availableModels={availableModels}
              onCreateSession={handleCreateSessionForChat}
              onSessionCreated={handleSessionCreatedForChat}
              onFilesFullChange={setSessionFilesOwnsBoundary}
              sessionFilesHandleRef={sessionFilesHandleRef}
              workspaceFilesHandleRef={captureWorkspaceFilesHandle}
              onStartEdgeCollapseSidebar={() => setIsSidebarCollapsed(true)}
              scrollTarget={sessionScrollTarget}
              activeSlotSessionIds={activeSlotSessionIds}
              workspaceFileTarget={workspaceFileTarget}
              workspaceTargetResolving={workspaceTargetResolving}
              onWorkspaceFilesClose={handleWorkspaceFilesClose}
              onWorkspaceTabSelect={handleWorkspaceTabSelect}
            />
          </div>

          {(activeSurface === 'skills' || visitedPrimarySurfaces.has('skills')) && (
            <div
              data-testid="skills-primary-surface"
              aria-hidden={activeSurface !== 'skills'}
              className={activeSurface === 'skills' ? 'flex min-w-0 flex-1' : 'hidden'}
            >
              <SkillsPage />
            </div>
          )}

          {(activeSurface === 'schedule' || visitedPrimarySurfaces.has('schedule')) && (
            <div
              data-testid="schedule-primary-surface"
              aria-hidden={activeSurface !== 'schedule'}
              className={activeSurface === 'schedule' ? 'flex min-w-0 flex-1' : 'hidden'}
            >
              <SchedulePage
                unreadCount={cronUnreadCount}
                onUnreadChange={setCronUnreadCount}
              />
            </div>
          )}

          {(activeSurface === 'connections' || visitedPrimarySurfaces.has('connections')) && (
            <div
              key={connectionsResetKey}
              data-testid="connections-primary-surface"
              aria-hidden={activeSurface !== 'connections'}
              className={activeSurface === 'connections' ? 'flex min-w-0 flex-1' : 'hidden'}
            >
              <ConnectionsPage
                active={activeSurface === 'connections'}
                onDirtyChange={setConnectionsHaveUnsavedChanges}
                onPermissionsInvalidated={() => setPermissionsRefreshToken((token) => token + 1)}
              />
            </div>
          )}
        </div>
      </div>
      {/* Settings modal */}
      {panelMounted && (
        <>
          <div
            className={`fixed inset-0 z-20 bg-[rgba(35,30,23,0.42)] backdrop-blur-sm transition-opacity duration-200 ${
              activePanel ? 'opacity-100' : 'opacity-0'
            }`}
            onClick={requestCloseConfigPanel}
            onTransitionEnd={() => { if (!activePanel) setPanelMounted(false); }}
          />
          <div
            className={`fixed inset-0 z-30 flex items-center justify-center p-6 transition-[opacity,transform] duration-200 ease-out ${
              activePanel ? 'opacity-100 scale-100' : 'opacity-0 scale-[0.98] pointer-events-none'
            }`}
            onClick={requestCloseConfigPanel}
          >
            <div
              ref={settingsDialogRef}
              role="dialog"
              aria-modal="true"
              aria-label="设置"
              tabIndex={-1}
              onKeyDown={handleSettingsDialogKeyDown}
              className="max-h-[88vh] overflow-hidden rounded-[22px] shadow-[0_30px_80px_rgba(20,16,10,0.35)] outline-none"
              style={{
                width: 'min(980px, calc(100vw - 48px))',
                height: 'min(760px, 88vh)',
              }}
              onClick={(e) => e.stopPropagation()}
            >
              <SettingsCenter
                onClose={requestCloseConfigPanel}
                onUnsavedChangesChange={setSettingsHasUnsavedChanges}
                permissionsRefreshToken={permissionsRefreshToken}
              />
            </div>
          </div>
        </>
      )}
      {unsavedConfirmSource && (
            <div
              className="fixed inset-0 z-40 flex items-center justify-center bg-[rgba(35,30,23,0.30)] px-4"
              onClick={cancelUnsavedConfirm}
            >
              <div
                ref={unsavedConfirmDialogRef}
                role="alertdialog"
                aria-modal="true"
                aria-labelledby="unsaved-confirm-title"
                aria-describedby="unsaved-confirm-description"
                tabIndex={-1}
                onKeyDown={handleUnsavedConfirmKeyDown}
                onClick={(event) => event.stopPropagation()}
                className="w-[min(420px,calc(100vw-32px))] rounded-2xl border border-[#e8e3d9] bg-[#fffdf9] p-5 text-[#1c1a16] shadow-[0_22px_70px_rgba(20,16,10,0.30)] outline-none"
              >
                <div
                  id="unsaved-confirm-title"
                  className="text-[16px] font-bold text-[#1c1a16]"
                >
                  {unsavedConfirmSource === 'connections'
                    ? '放弃未保存的连接修改？'
                    : '放弃未保存的修改？'}
                </div>
                <p
                  id="unsaved-confirm-description"
                  className="mt-2 text-[13.5px] leading-6 text-[#6f6960]"
                >
                  {unsavedConfirmSource === 'connections'
                    ? '切换页面后，当前连接表单或工具设置中的修改不会保存。'
                    : '关闭后，本次编辑的内容不会保存。'}
                </p>
                <div className="mt-5 flex justify-end gap-2">
                  <button
                    type="button"
                    onClick={cancelUnsavedConfirm}
                    className="h-9 rounded-[10px] border border-[#e8e3d9] bg-white px-4 text-[13px] font-semibold text-[#1c1a16] transition hover:bg-[#f6f2ea] focus:outline-none focus:ring-2 focus:ring-[#b8814a]/25"
                  >
                    继续编辑
                  </button>
                  <button
                    type="button"
                    onClick={discardUnsavedChanges}
                    className="h-9 rounded-[10px] bg-[#8a5a2f] px-4 text-[13px] font-semibold text-white transition hover:bg-[#714722] focus:outline-none focus:ring-2 focus:ring-[#b8814a]/30"
                  >
                    {unsavedConfirmSource === 'connections' ? '放弃并切换' : '放弃并关闭'}
                  </button>
                </div>
              </div>
            </div>
      )}
      {sessionSwitchSaveError && (
        <div data-testid="session-switch-save-error">
          <FeedbackMessage
            className="fixed bottom-5 left-1/2 z-50 w-[min(520px,calc(100vw-32px))] -translate-x-1/2 rounded-xl border border-red-200 bg-red-50 px-4 py-3 text-[13px] font-medium text-red-800 shadow-lg"
            tone="error"
            onDismiss={() => setSessionSwitchSaveError('')}
          >
            {sessionSwitchSaveError}
          </FeedbackMessage>
        </div>
      )}
    </div>
  );
}

function PublicRoute({ children }: { children: React.ReactNode }) {
  const isAuthenticated = apiService.isAuthenticated();

  if (isAuthenticated) {
    return <Navigate to="/" replace />;
  }

  return <>{children}</>;
}

function AdminLoginRoute({ children }: { children: React.ReactNode }) {
  if (apiService.isAuthenticated() && apiService.isAdminUser()) {
    return <Navigate to="/admin" replace />;
  }

  return <>{children}</>;
}

function UserRoute({ children }: { children: React.ReactNode }) {
  const isAuthenticated = apiService.isAuthenticated();

  if (!isAuthenticated) {
    return <Navigate to="/login" replace />;
  }

  return <>{children}</>;
}

function AdminRoute({ children }: { children: React.ReactNode }) {
  const isAuthenticated = apiService.isAuthenticated();
  const isAdmin = apiService.isAdminUser();

  if (!isAuthenticated) {
    return <Navigate to="/admin/login" replace />;
  }

  if (!isAdmin) {
    return <Navigate to="/admin/login" replace />;
  }

  return <>{children}</>;
}

function createAppRouter() {
  return createBrowserRouter([
    {
      path: '/login',
      element: (
        <PublicRoute>
          <Login />
        </PublicRoute>
      ),
    },
    {
      path: '/admin/login',
      element: (
        <AdminLoginRoute>
          <Login mode="admin" />
        </AdminLoginRoute>
      ),
    },
    {
      path: '/',
      element: (
        <UserRoute>
          <HomePage />
        </UserRoute>
      ),
      children: [
        { index: true, element: null },
        { path: 'schedule', element: null },
        { path: 'skills', element: null },
        { path: 'connections', element: null },
        { path: 'workspace', element: null },
      ],
    },
    {
      path: '/admin',
      element: (
        <AdminRoute>
          <AdminConsole />
        </AdminRoute>
      ),
    },
    { path: '*', element: <Navigate to="/" replace /> },
  ]);
}

let sharedAppRouter: ReturnType<typeof createAppRouter> | null = null;
let appRouterConsumers = 0;

function App() {
  if (!sharedAppRouter) sharedAppRouter = createAppRouter();
  const router = sharedAppRouter;

  useEffect(() => {
    appRouterConsumers += 1;
    return () => {
      appRouterConsumers -= 1;
      const routerToDispose = router;
      queueMicrotask(() => {
        // React StrictMode replays effects immediately. Deferring disposal lets
        // that replay retain one router while still releasing test/unmounted roots.
        if (appRouterConsumers === 0 && sharedAppRouter === routerToDispose) {
          routerToDispose.dispose();
          sharedAppRouter = null;
        }
      });
    };
  }, [router]);

  return <RouterProvider router={router} />;
}

export default App;
