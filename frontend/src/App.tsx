import { useState, useEffect, useCallback } from 'react';
import { BrowserRouter, Routes, Route, Navigate } from 'react-router-dom';
import { Login } from './components/Login';
import { SessionList } from './components/SessionList';
import { ChatV2 } from './components/ChatV2';
import AgentConfig from './components/AgentConfig';
import CronHistory from './components/CronHistory';
import SkillManager from './components/SkillManager';
import { apiService } from './services/api';
import type { ModelInfo } from './types';

type ConfigPanel = 'config' | 'skills' | 'cron' | null;

// 主页面组件
function HomePage() {
  const [currentSessionId, setCurrentSessionId] = useState<string>('');
  const [refreshTrigger, setRefreshTrigger] = useState(0);
  const [executingSessionId, setExecutingSessionId] = useState<string | null>(null);
  const [isSidebarCollapsed, setIsSidebarCollapsed] = useState(false);
  const [selectedModelId, setSelectedModelId] = useState<string>('');
  const [availableModels, setAvailableModels] = useState<ModelInfo[]>([]);
  const [activePanel, setActivePanel] = useState<ConfigPanel>(null);
  const [panelMounted, setPanelMounted] = useState(false);

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

  // 刷新会话列表的回调
  const handleTitleUpdated = () => {
    setRefreshTrigger((prev) => prev + 1);
  };

  // 执行状态回调
  const handleExecutionStart = (sessionId: string) => {
    setExecutingSessionId(sessionId);
  };

  const handleExecutionEnd = () => {
    setExecutingSessionId(null);
  };

  // 🆕 处理检测到运行中会话的回调
  const handleRunningSessionDetected = (sessionId: string) => {
    console.log(`🔄 自动选择运行中的会话: ${sessionId}`);
    setExecutingSessionId(sessionId);
    // 自动选择该会话，让用户能看到运行状态
    if (!currentSessionId) {
      setCurrentSessionId(sessionId);
    }
  };

  // 🆕 从 ChatV2 欢迎页触发创建会话（输入即创建）
  const handleCreateSessionForChat = useCallback(async (modelId?: string): Promise<string> => {
    const response = await apiService.createSession(modelId);
    setRefreshTrigger((prev) => prev + 1); // 刷新侧边栏列表
    setCurrentSessionId(response.session_id);
    return response.session_id;
  }, []);

  return (
    <div className="flex h-screen overflow-hidden">
      <SessionList
        currentSessionId={currentSessionId}
        onSessionSelect={setCurrentSessionId}
        refreshTrigger={refreshTrigger}
        executingSessionId={executingSessionId}
        onRunningSessionDetected={handleRunningSessionDetected}
        isCollapsed={isSidebarCollapsed}
        onModelChange={setSelectedModelId}
        onNewChat={() => setCurrentSessionId('')}
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
            className={`fixed top-0 right-0 bottom-0 w-[380px] bg-claude-bg border-l border-claude-border z-30 transition-transform duration-300 ease-out shadow-xl ${
              activePanel ? 'translate-x-0' : 'translate-x-full'
            }`}
          >
            {activePanel === 'config' && <AgentConfig onClose={closeConfigPanel} />}
            {activePanel === 'skills' && <SkillManager onClose={closeConfigPanel} />}
            {activePanel === 'cron' && <CronHistory onClose={closeConfigPanel} />}
          </div>
        </>
      )}
    </div>
  );
}

// 路由守卫组件
function ProtectedRoute({ children }: { children: React.ReactNode }) {
  const isAuthenticated = apiService.isAuthenticated();

  if (!isAuthenticated) {
    return <Navigate to="/login" replace />;
  }

  return <>{children}</>;
}

function App() {
  return (
    <BrowserRouter>
      <Routes>
        <Route path="/login" element={<Login />} />
        <Route
          path="/"
          element={
            <ProtectedRoute>
              <HomePage />
            </ProtectedRoute>
          }
        />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </BrowserRouter>
  );
}

export default App;
