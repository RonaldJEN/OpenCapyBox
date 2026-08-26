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
import AdminConsole from './components/AdminConsole';
import SettingsCenter from './components/SettingsCenter';
import SkillsPage from './components/SkillsPage';
import ConnectionsPage from './components/ConnectionsPage';
import SchedulePage from './components/SchedulePage';
import { apiService } from './services/api';
import { getUnreadCount } from './services/configApi';
import { ChatRuntimeProvider, useChatRuntime } from './runtime/ChatRuntimeProvider';
import { SessionStatus, type ModelInfo, type Session } from './types';

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
  const {
    getActiveSlotSessionIds,
    getExecutingSessionIds,
    syncRunningSessions,
  } = useChatRuntime();
  const [currentSessionId, setCurrentSessionId] = useState<string>('');
  const [isSidebarCollapsed, setIsSidebarCollapsed] = useState(false);
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
  const sessionNavigationRequestEpochRef = useRef(0);
  const initialRunningSessionsHandledRef = useRef(false);
  const pendingSurfaceActionRef = useRef<PendingSurfaceAction | null>(null);
  const mainContentRef = useRef<HTMLDivElement>(null);
  const settingsDialogRef = useRef<HTMLDivElement>(null);
  const unsavedConfirmDialogRef = useRef<HTMLDivElement>(null);
  const confirmReturnFocusRef = useRef<HTMLElement | null>(null);
  currentSessionIdRef.current = currentSessionId;
  const executingSessionIds = getExecutingSessionIds();
  const activeSlotSessionIds = getActiveSlotSessionIds();
  const effectiveSidebarCollapsed = isSidebarCollapsed;
  const unsavedConfirmSource: DirtySource | null = pendingNavigation?.source
    ?? (connectionsNavigationBlocker.state === 'blocked' ? 'connections' : null);

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
      setAvailableModels(res.models);
      setSelectedModelId((current) => current || res.default_model);
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

  const saveThenSelectSession = useCallback(async (
    sessionId: string,
    target?: { roundId: string },
  ) => {
    const requestEpoch = ++sessionNavigationRequestEpochRef.current;
    const ownerSessionId = currentSessionIdRef.current;
    setSessionSwitchSaveError('');

    if (ownerSessionId && ownerSessionId !== sessionId) {
      const filesHandle = sessionFilesHandleRef.current;
      if (!filesHandle || filesHandle.ownerSessionId !== ownerSessionId) {
        setSessionSwitchSaveError('文件编辑状态仍在同步，已留在当前会话。请稍后重试。');
        return;
      }
      const expectedOwner = {
        ownerSessionId,
        ownerEpoch: filesHandle.ownerEpoch,
      };
      if (filesHandle.hasDirty(expectedOwner)) {
        let result: Awaited<ReturnType<ArtifactsPanelHandle['saveDirty']>>;
        try {
          result = await filesHandle.saveDirty(expectedOwner);
        } catch (error) {
          console.error('Failed to save dirty Session files before navigation:', error);
          if (
            requestEpoch === sessionNavigationRequestEpochRef.current
            && currentSessionIdRef.current === ownerSessionId
          ) {
            setSessionSwitchSaveError('文件保存失败，已留在当前会话。请处理保存错误后重试。');
          }
          return;
        }
        if (
          requestEpoch !== sessionNavigationRequestEpochRef.current
          || currentSessionIdRef.current !== ownerSessionId
        ) return;
        if (
          !result.ok
          || result.stale
          || result.ownerSessionId !== expectedOwner.ownerSessionId
          || result.ownerEpoch !== expectedOwner.ownerEpoch
        ) {
          setSessionSwitchSaveError('文件保存失败，已留在当前会话。请处理保存错误后重试。');
          return;
        }
      }
    }

    if (
      requestEpoch !== sessionNavigationRequestEpochRef.current
      || currentSessionIdRef.current !== ownerSessionId
    ) return;
    applySessionSelection(sessionId, target);
  }, [applySessionSelection]);

  const handleSessionSelect = useCallback((sessionId: string, target?: { roundId: string }) => {
    requestPrimarySurface('chat', () => {
      void saveThenSelectSession(sessionId, target);
    });
  }, [requestPrimarySurface, saveThenSelectSession]);

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
          userId={apiService.getUserId() || 'user'}
          onCollapsedChange={setIsSidebarCollapsed}
        >
          <SessionList
            currentSessionId={currentSessionId}
            onSessionSelect={handleSessionSelect}
            refreshTrigger={refreshTrigger}
            optimisticSession={optimisticSession}
            executingSessionIds={executingSessionIds}
            isCollapsed={effectiveSidebarCollapsed}
            onModelChange={setSelectedModelId}
            onNewChat={() => {
              requestPrimarySurface('chat', () => {
                void saveThenSelectSession('');
              });
            }}
            cronUnreadCount={cronUnreadCount}
            onOpenConfig={toggleSettingsPanel}
            onOpenCron={() => requestPrimarySurface('schedule')}
            activePrimarySurface={activeSurface}
            onOpenSkills={() => requestPrimarySurface('skills')}
            onOpenConnections={() => requestPrimarySurface('connections')}
          />
        </AppSidebar>
        <div className="relative flex min-w-0 flex-1 overflow-hidden">
          {!activePanel && (
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
                  onClick={() => requestPrimarySurface(item.id)}
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
              onStartEdgeCollapseSidebar={() => setIsSidebarCollapsed(true)}
              scrollTarget={sessionScrollTarget}
              activeSlotSessionIds={activeSlotSessionIds}
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
        <div
          role="alert"
          aria-live="assertive"
          data-testid="session-switch-save-error"
          className="fixed bottom-5 left-1/2 z-50 w-[min(520px,calc(100vw-32px))] -translate-x-1/2 rounded-xl border border-red-200 bg-red-50 px-4 py-3 text-[13px] font-medium text-red-800 shadow-lg"
        >
          {sessionSwitchSaveError}
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
