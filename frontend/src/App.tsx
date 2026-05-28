import { useState, useEffect, useCallback, useRef } from 'react';
import { BrowserRouter, Routes, Route, Navigate } from 'react-router-dom';
import { Login } from './components/Login';
import { SessionList } from './components/SessionList';
import { ChatV2 } from './components/ChatV2';
import AdminConsole from './components/AdminConsole';
import AgentConfig from './components/AgentConfig';
import CronSchedule from './components/CronSchedule';
import SkillManager from './components/SkillManager';
import { apiService } from './services/api';
import { getUnreadCount } from './services/configApi';
import type { ModelInfo } from './types';

type ConfigPanel = 'config' | 'skills' | 'cron' | null;
type SessionScrollTarget = {
  sessionId: string;
  roundId: string;
  nonce: number;
};

const RUNNING_SESSIONS_RECONCILE_INTERVAL_MS = 5000;

const sameStringSet = (left: Set<string>, right: Set<string>) => (
  left.size === right.size && Array.from(left).every((item) => right.has(item))
);

// 主页面组件
function HomePage() {
  const [currentSessionId, setCurrentSessionId] = useState<string>('');
  const [refreshTrigger, setRefreshTrigger] = useState(0);
  const [executingSessionIds, setExecutingSessionIds] = useState<Set<string>>(() => new Set());
  const [activeSlotSessionIds, setActiveSlotSessionIds] = useState<Set<string>>(() => new Set());
  const [isSidebarCollapsed, setIsSidebarCollapsed] = useState(false);
  const [selectedModelId, setSelectedModelId] = useState<string>('');
  const [availableModels, setAvailableModels] = useState<ModelInfo[]>([]);
  const [activePanel, setActivePanel] = useState<ConfigPanel>(null);
  const [panelMounted, setPanelMounted] = useState(false);
  const [cronUnreadCount, setCronUnreadCount] = useState(0);
  const [sessionScrollTarget, setSessionScrollTarget] = useState<SessionScrollTarget | null>(null);
  const sessionScrollNonceRef = useRef(0);
  const currentSessionIdRef = useRef(currentSessionId);
  const initialRunningSessionsHandledRef = useRef(false);
  currentSessionIdRef.current = currentSessionId;

  const closeConfigPanel = useCallback(() => {
    setActivePanel(null);
    // 打开配置抽屉时会折叠左侧栏，关闭时需要恢复。
    setIsSidebarCollapsed(false);
  }, []);

  // activePanel 变化时控制 mount
  useEffect(() => {
    if (activePanel) setPanelMounted(true);
  }, [activePanel]);

  // 統一加載模型列表（只請求一次）
  useEffect(() => {
    apiService.getModels().then((res) => {
      setAvailableModels(res.models);
      if (!selectedModelId) {
        setSelectedModelId(res.default_model);
      }
    }).catch((err) => {
      console.error('Failed to load models:', err);
    });
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  // 加载 Cron 未读计数 + 60s 轮询
  useEffect(() => {
    const fetchUnread = () => {
      getUnreadCount().then((r) => setCronUnreadCount(r.count)).catch(() => {});
    };
    fetchUnread();
    const timer = setInterval(fetchUnread, 60000);
    return () => clearInterval(timer);
  }, []);

  // 刷新会话列表的回调
  const handleTitleUpdated = () => {
    setRefreshTrigger((prev) => prev + 1);
  };

  // 执行状态回调
  const handleExecutionStart = (sessionId: string) => {
    setExecutingSessionIds((prev) => {
      if (prev.has(sessionId)) return prev;
      const next = new Set(prev);
      next.add(sessionId);
      return next;
    });
    setActiveSlotSessionIds((prev) => {
      if (prev.has(sessionId)) return prev;
      const next = new Set(prev);
      next.add(sessionId);
      return next;
    });
  };

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

  const handleExecutionEnd = (sessionId?: string) => {
    if (sessionId) {
      setExecutingSessionIds((prev) => {
        if (!prev.has(sessionId)) return prev;
        const next = new Set(prev);
        next.delete(sessionId);
        return next;
      });
      setActiveSlotSessionIds((prev) => {
        if (!prev.has(sessionId)) return prev;
        const next = new Set(prev);
        next.delete(sessionId);
        return next;
      });
    } else {
      setExecutingSessionIds(new Set());
      setActiveSlotSessionIds(new Set());
    }
  };

  const reconcileRunningSessions = useCallback(async () => {
    try {
      const result = await apiService.getRunningSessions();
      const sessionIds = result.running_sessions.map((item) => item.session_id);
      const nextSessionIds = new Set(sessionIds);

      setExecutingSessionIds((prev) => (
        sameStringSet(prev, nextSessionIds) ? prev : new Set(nextSessionIds)
      ));
      setActiveSlotSessionIds((prev) => (
        sameStringSet(prev, nextSessionIds) ? prev : new Set(nextSessionIds)
      ));

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
  }, []);

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
    setCurrentSessionId(response.session_id);
    setSessionScrollTarget(null);
    return response.session_id;
  }, []);

  return (
    <div className="flex h-screen overflow-hidden">
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
        onOpenConfig={() => {
          const next = activePanel === 'config' ? null : 'config';
          setActivePanel(next);
          setIsSidebarCollapsed(!!next);
        }}
        onOpenSkills={() => {
          const next = activePanel === 'skills' ? null : 'skills';
          setActivePanel(next);
          setIsSidebarCollapsed(!!next);
        }}
        onOpenCron={() => {
          const next = activePanel === 'cron' ? null : 'cron';
          setActivePanel(next);
          setIsSidebarCollapsed(!!next);
        }}
      />
      <ChatV2
        sessionId={currentSessionId}
        onTitleUpdated={handleTitleUpdated}
        onExecutionStart={handleExecutionStart}
        onExecutionEnd={handleExecutionEnd}
        onPanelToggle={setIsSidebarCollapsed}
        selectedModelId={selectedModelId}
        onModelChange={setSelectedModelId}
        availableModels={availableModels}
        onCreateSession={handleCreateSessionForChat}
        scrollTarget={sessionScrollTarget}
        activeSlotSessionIds={activeSlotSessionIds}
      />
      {/* Config panel overlay drawer */}
      {panelMounted && (
        <>
          <div
            className={`fixed inset-0 z-20 bg-black/10 transition-opacity duration-200 ${
              activePanel ? 'opacity-100' : 'opacity-0'
            }`}
            onClick={closeConfigPanel}
            onTransitionEnd={() => { if (!activePanel) setPanelMounted(false); }}
          />
          <div
            className={`fixed top-0 right-0 bottom-0 bg-claude-bg border-l border-claude-border z-30 transition-transform duration-300 ease-out shadow-xl ${
              activePanel === 'cron' ? 'w-[760px]' : 'w-[380px]'
            } ${
              activePanel ? 'translate-x-0' : 'translate-x-full'
            }`}
          >
            {activePanel === 'config' && <AgentConfig onClose={closeConfigPanel} />}
            {activePanel === 'skills' && <SkillManager onClose={closeConfigPanel} />}
            {activePanel === 'cron' && <CronSchedule onClose={closeConfigPanel} unreadCount={cronUnreadCount} onUnreadChange={setCronUnreadCount} />}
          </div>
        </>
      )}
    </div>
  );
}

function PublicRoute({ children }: { children: React.ReactNode }) {
  const isAuthenticated = apiService.isAuthenticated();

  if (isAuthenticated) {
    return <Navigate to={apiService.isAdminUser() ? '/admin' : '/'} replace />;
  }

  return <>{children}</>;
}

function UserRoute({ children }: { children: React.ReactNode }) {
  const isAuthenticated = apiService.isAuthenticated();

  if (!isAuthenticated) {
    return <Navigate to="/login" replace />;
  }

  if (apiService.isAdminUser()) {
    return <Navigate to="/admin" replace />;
  }

  return <>{children}</>;
}

function AdminRoute({ children }: { children: React.ReactNode }) {
  const isAuthenticated = apiService.isAuthenticated();
  const isAdmin = apiService.isAdminUser();

  if (!isAuthenticated) {
    return <Navigate to="/login" replace />;
  }

  if (!isAdmin) {
    return <Navigate to="/" replace />;
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
