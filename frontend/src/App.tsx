import { Dispatch, SetStateAction, useState, useEffect, useCallback, useRef, type KeyboardEvent } from 'react';
import { BrowserRouter, Routes, Route, Navigate } from 'react-router-dom';
import { Login } from './components/Login';
import { SessionList } from './components/SessionList';
import { ChatV2 } from './components/ChatV2';
import AdminConsole from './components/AdminConsole';
import CronSchedule from './components/CronSchedule';
import SettingsCenter from './components/SettingsCenter';
import { apiService } from './services/api';
import { getUnreadCount } from './services/configApi';
import { ChatRuntimeProvider, useChatRuntime } from './runtime/ChatRuntimeProvider';
import type { ModelInfo } from './types';

type ConfigPanel = 'config' | 'cron' | null;
type SessionScrollTarget = {
  sessionId: string;
  roundId: string;
  nonce: number;
};

const RUNNING_SESSIONS_RECONCILE_INTERVAL_MS = 5000;

// 主页面组件
function HomePage() {
  const [refreshTrigger, setRefreshTrigger] = useState(0);

  const handleTitleUpdated = useCallback(() => {
    setRefreshTrigger((prev) => prev + 1);
  }, []);

  return (
    <ChatRuntimeProvider onTitleUpdated={handleTitleUpdated}>
      <HomePageContent refreshTrigger={refreshTrigger} setRefreshTrigger={setRefreshTrigger} />
    </ChatRuntimeProvider>
  );
}

interface HomePageContentProps {
  refreshTrigger: number;
  setRefreshTrigger: Dispatch<SetStateAction<number>>;
}

function HomePageContent({ refreshTrigger, setRefreshTrigger }: HomePageContentProps) {
  const {
    getActiveSlotSessionIds,
    getExecutingSessionIds,
    syncRunningSessions,
  } = useChatRuntime();
  const [currentSessionId, setCurrentSessionId] = useState<string>('');
  const [isSidebarCollapsed, setIsSidebarCollapsed] = useState(false);
  const [selectedModelId, setSelectedModelId] = useState<string>('');
  const [availableModels, setAvailableModels] = useState<ModelInfo[]>([]);
  const [activePanel, setActivePanel] = useState<ConfigPanel>(null);
  const [lastActivePanel, setLastActivePanel] = useState<Exclude<ConfigPanel, null> | null>(null);
  const [panelMounted, setPanelMounted] = useState(false);
  const [settingsHasUnsavedChanges, setSettingsHasUnsavedChanges] = useState(false);
  const [unsavedConfirmOpen, setUnsavedConfirmOpen] = useState(false);
  const [pendingPanelAfterConfirm, setPendingPanelAfterConfirm] = useState<ConfigPanel | undefined>(undefined);
  const [cronUnreadCount, setCronUnreadCount] = useState(0);
  const [sessionScrollTarget, setSessionScrollTarget] = useState<SessionScrollTarget | null>(null);
  const sessionScrollNonceRef = useRef(0);
  const currentSessionIdRef = useRef(currentSessionId);
  const initialRunningSessionsHandledRef = useRef(false);
  const mainContentRef = useRef<HTMLDivElement>(null);
  const settingsDialogRef = useRef<HTMLDivElement>(null);
  const unsavedConfirmDialogRef = useRef<HTMLDivElement>(null);
  currentSessionIdRef.current = currentSessionId;
  const executingSessionIds = getExecutingSessionIds();
  const activeSlotSessionIds = getActiveSlotSessionIds();
  const renderedPanel = activePanel ?? lastActivePanel;

  const applyActivePanel = useCallback((nextPanel: ConfigPanel) => {
    if (activePanel === 'config' && nextPanel !== 'config') {
      setSettingsHasUnsavedChanges(false);
    }
    setActivePanel(nextPanel);
    setIsSidebarCollapsed(nextPanel === 'cron');
  }, [activePanel]);

  const requestActivePanel = useCallback((nextPanel: ConfigPanel) => {
    if (nextPanel !== activePanel && activePanel === 'config' && settingsHasUnsavedChanges) {
      setPendingPanelAfterConfirm(nextPanel);
      setUnsavedConfirmOpen(true);
      return;
    }

    applyActivePanel(nextPanel);
  }, [activePanel, applyActivePanel, settingsHasUnsavedChanges]);

  const requestCloseConfigPanel = useCallback(() => {
    requestActivePanel(null);
  }, [requestActivePanel]);

  const togglePanel = useCallback((panel: Exclude<ConfigPanel, null>) => {
    requestActivePanel(activePanel === panel ? null : panel);
  }, [activePanel, requestActivePanel]);

  // activePanel 变化时控制 mount
  useEffect(() => {
    if (activePanel) {
      setLastActivePanel(activePanel);
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
    if (unsavedConfirmOpen) {
      unsavedConfirmDialogRef.current?.focus();
    }
  }, [unsavedConfirmOpen]);

  const cancelUnsavedConfirm = useCallback(() => {
    setUnsavedConfirmOpen(false);
    setPendingPanelAfterConfirm(undefined);
    settingsDialogRef.current?.focus();
  }, []);

  const discardUnsavedChanges = useCallback(() => {
    const nextPanel = pendingPanelAfterConfirm ?? null;
    setUnsavedConfirmOpen(false);
    setPendingPanelAfterConfirm(undefined);
    applyActivePanel(nextPanel);
  }, [applyActivePanel, pendingPanelAfterConfirm]);

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

    if (event.key === 'Enter') {
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

  const handleSessionSelect = (sessionId: string, target?: { roundId: string }) => {
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
  };

  const reconcileRunningSessions = useCallback(async () => {
    try {
      const result = await apiService.getRunningSessions();
      const sessionIds = result.running_sessions.map((item) => item.session_id);
      syncRunningSessions(result.running_sessions);

      if (!initialRunningSessionsHandledRef.current) {
        initialRunningSessionsHandledRef.current = true;
        if (!currentSessionIdRef.current && sessionIds.length > 0) {
          console.log(`🔄 检测到运行中的会话: ${sessionIds.join(', ')}`);
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
    setRefreshTrigger((prev) => prev + 1); // 刷新侧边栏列表
    return response.session_id;
  }, [setRefreshTrigger]);

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
        <SessionList
          currentSessionId={currentSessionId}
          onSessionSelect={handleSessionSelect}
          refreshTrigger={refreshTrigger}
          executingSessionIds={executingSessionIds}
          isCollapsed={isSidebarCollapsed}
          onModelChange={setSelectedModelId}
          onNewChat={() => {
            setCurrentSessionId('');
            setSessionScrollTarget(null);
          }}
          cronUnreadCount={cronUnreadCount}
          onOpenConfig={() => togglePanel('config')}
          onOpenCron={() => togglePanel('cron')}
        />
        <ChatV2
          sessionId={currentSessionId}
          onPanelToggle={setIsSidebarCollapsed}
          selectedModelId={selectedModelId}
          onModelChange={setSelectedModelId}
          availableModels={availableModels}
          onCreateSession={handleCreateSessionForChat}
          onSessionCreated={handleSessionCreatedForChat}
          scrollTarget={sessionScrollTarget}
          activeSlotSessionIds={activeSlotSessionIds}
        />
      </div>
      {/* Config panel overlay drawer */}
      {panelMounted && (
        <>
          <div
            className={`fixed inset-0 z-20 transition-opacity duration-200 ${
              activePanel ? 'opacity-100' : 'opacity-0'
            } ${renderedPanel === 'cron' ? '' : 'backdrop-blur-sm'}`}
            style={{ background: renderedPanel === 'cron' ? 'rgba(0, 0, 0, 0.10)' : 'rgba(35, 30, 23, 0.42)' }}
            onClick={requestCloseConfigPanel}
            onTransitionEnd={() => { if (!activePanel) setPanelMounted(false); }}
          />
          {renderedPanel === 'cron' ? (
            <div
              className={`fixed top-0 right-0 bottom-0 bg-claude-bg border-l border-claude-border z-30 transition-transform duration-300 ease-out shadow-xl ${
                activePanel ? 'translate-x-0' : 'translate-x-full'
              }`}
              style={{ width: 'min(1040px, calc(100vw - 48px))' }}
            >
              <CronSchedule onClose={requestCloseConfigPanel} unreadCount={cronUnreadCount} onUnreadChange={setCronUnreadCount} />
            </div>
          ) : (
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
                />
              </div>
            </div>
          )}
          {unsavedConfirmOpen && (
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
                  放弃未保存的修改？
                </div>
                <p
                  id="unsaved-confirm-description"
                  className="mt-2 text-[13.5px] leading-6 text-[#6f6960]"
                >
                  关闭后，本次编辑的内容不会保存。
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
                    放弃并关闭
                  </button>
                </div>
              </div>
            </div>
          )}
        </>
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

function App() {
  return (
    <BrowserRouter>
      <Routes>
        <Route
          path="/login"
          element={
            <PublicRoute>
              <Login />
            </PublicRoute>
          }
        />
        <Route
          path="/admin/login"
          element={
            <AdminLoginRoute>
              <Login mode="admin" />
            </AdminLoginRoute>
          }
        />
        <Route
          path="/"
          element={
            <UserRoute>
              <HomePage />
            </UserRoute>
          }
        />
        <Route
          path="/admin"
          element={
            <AdminRoute>
              <AdminConsole />
            </AdminRoute>
          }
        />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </BrowserRouter>
  );
}

export default App;
