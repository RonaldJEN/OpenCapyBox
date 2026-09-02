import { useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
import { afterAll, afterEach, beforeAll, beforeEach, describe, it, expect, vi } from 'vitest';
import { act, render, screen, fireEvent, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import App from '../../App';
import { apiService } from '../../services/api';
import { emitWorkspaceMutation, subscribeWorkspaceMutation } from '../../services/workspaceEvents';

const NativeRequest = globalThis.Request;

class RouterTestRequest extends NativeRequest {
  constructor(input: RequestInfo | URL, init?: RequestInit) {
    // jsdom and Node's undici expose distinct AbortSignal implementations.
    // These routes have no loaders, so keep the native Request signal while
    // still exercising the real data-router PUSH/POP blocker state machine.
    super(input, init ? { ...init, signal: undefined } : init);
  }
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, reject, resolve };
}

const mockControls = vi.hoisted(() => ({
  resolveRunningDuringManualSelect: null as (() => void) | null,
  chatMountCount: 0,
  chatAbortCount: 0,
  chatController: null as AbortController | null,
  sessionFilesDirty: false,
  sessionFilesSaveCalls: [] as Array<{ ownerSessionId: string; ownerEpoch: number }>,
  sessionFilesSaveImpl: null as null | ((owner: { ownerSessionId: string; ownerEpoch: number }) => Promise<{
    ownerSessionId: string;
    ownerEpoch: number;
    ok: boolean;
    stale: boolean;
    failedPaths: string[];
  }>),
  workspaceGet: vi.fn(),
  cronRunsGet: vi.fn(),
}));

vi.mock('../../services/api', () => ({
  apiService: {
    isAuthenticated: vi.fn(() => true),
    getUserId: vi.fn(() => 'demo-user'),
    isAdminUser: vi.fn(() => false),
    getAxiosClient: vi.fn(() => ({
      get: mockControls.workspaceGet,
      post: vi.fn(),
      patch: vi.fn(),
      put: vi.fn(),
      delete: vi.fn(),
    })),
    getModels: vi.fn().mockResolvedValue({
      models: [{ id: 'test-model', name: 'Test Model' }],
      default_model: 'test-model',
      subagent_default_model: 'test-model',
    }),
    createSession: vi.fn().mockResolvedValue({
      session_id: 'new-session',
    }),
    getRunningSessions: vi.fn().mockResolvedValue({ running_sessions: [] }),
  },
}));

vi.mock('../../services/configApi', () => ({
  getUnreadCount: vi.fn().mockResolvedValue({ count: 0 }),
  getCronRuns: (...args: unknown[]) => mockControls.cronRunsGet(...args),
}));

vi.mock('../../components/SessionList', () => ({
  SessionList: ({ isCollapsed, onOpenConfig, onOpenCron, onOpenSkills, onOpenConnections, activePrimarySurface, onSessionSelect, onModelChange, onNewChat, executingSessionIds, sidebarMode, onSidebarModeChange, onOpenWorkspaceEntry, mobileSheet, onCloseMobileSheet }: any) => (
    <div>
      <div data-testid="sidebar-state">{isCollapsed ? 'collapsed' : 'open'}</div>
      <div data-testid="executing-sessions">{Array.from(executingSessionIds ?? []).join(',')}</div>
      <div data-testid="active-primary-surface">{activePrimarySurface}</div>
      <div data-testid={mobileSheet ? 'mobile-sidebar-mode' : 'sidebar-mode'}>{sidebarMode}</div>
      <button onClick={onOpenConfig}>open-config</button>
      <button onClick={onOpenCron}>open-cron</button>
      <button onClick={onOpenSkills}>open-skills</button>
      <button onClick={onOpenConnections}>open-connections</button>
      <button onClick={() => {
        onSessionSelect?.('manual-session');
        mockControls.resolveRunningDuringManualSelect?.();
      }}>select-manual</button>
      <button onClick={() => onSessionSelect?.('session-a')}>select-session-a</button>
      <button onClick={() => onSessionSelect?.('session-b')}>select-session-b</button>
      <button onClick={onNewChat}>new-chat</button>
      <button onClick={() => onModelChange?.('qwen-plus')}>select-qwen-model</button>
      <button onClick={() => onSidebarModeChange?.('workspace')}>sidebar-workspace</button>
      <button onClick={() => onSidebarModeChange?.('sessions')}>sidebar-sessions</button>
      <button onClick={() => onOpenWorkspaceEntry?.({ entry_id: 'workspace-file', parent_id: null, name: 'report.md', kind: 'file', path: 'report.md', size_bytes: 10, mime_type: 'text/markdown', sha256: 'hash', revision: 1, status: 'active', created_at: 'now', updated_at: 'now' })}>open-workspace-file</button>
      {mobileSheet && <button onClick={onCloseMobileSheet}>mock-close-mobile-sidebar</button>}
    </div>
  ),
}));

vi.mock('../../components/ChatV2', () => ({
  ChatV2: ({
    sessionId,
    selectedModelId,
    activeSlotSessionIds,
    onCreateSession,
    onSessionCreated,
    onFilesFullChange,
    sessionFilesHandleRef,
    workspaceFilesHandleRef,
    workspaceFileTarget,
    workspaceTargetResolving,
    onWorkspaceFilesClose,
  }: {
    sessionId?: string;
    selectedModelId?: string;
    activeSlotSessionIds?: Set<string>;
    onCreateSession?: (modelId?: string) => Promise<string>;
    onSessionCreated?: (sessionId: string) => void;
    onFilesFullChange?: (full: boolean) => void;
    sessionFilesHandleRef?: { current: any };
    workspaceFilesHandleRef?: ((handle: any) => void) | { current: any };
    workspaceFileTarget?: { entry_id?: string } | null;
    workspaceTargetResolving?: boolean;
    onWorkspaceFilesClose?: () => void;
  }) => {
    const [draft, setDraft] = useState('');
    const [waiting, setWaiting] = useState(false);
    const [filesFull, setFilesFull] = useState(false);
    const previousFilesOwnerRef = useRef({ ownerSessionId: sessionId || '', ownerEpoch: 0 });
    const currentFilesOwner = useMemo(() => (
      previousFilesOwnerRef.current.ownerSessionId === (sessionId || '')
        ? previousFilesOwnerRef.current
        : {
          ownerSessionId: sessionId || '',
          ownerEpoch: previousFilesOwnerRef.current.ownerEpoch + 1,
        }
    ), [sessionId]);

    useLayoutEffect(() => {
      previousFilesOwnerRef.current = currentFilesOwner;
      if (!sessionFilesHandleRef) return undefined;
      const handle = {
        ...currentFilesOwner,
        hasDirty: (expectedOwner: { ownerSessionId: string; ownerEpoch: number }) => (
          mockControls.sessionFilesDirty
          && expectedOwner.ownerSessionId === currentFilesOwner.ownerSessionId
          && expectedOwner.ownerEpoch === currentFilesOwner.ownerEpoch
        ),
        saveDirty: async (expectedOwner: { ownerSessionId: string; ownerEpoch: number }) => {
          mockControls.sessionFilesSaveCalls.push(expectedOwner);
          if (mockControls.sessionFilesSaveImpl) {
            return mockControls.sessionFilesSaveImpl(expectedOwner);
          }
          mockControls.sessionFilesDirty = false;
          return { ...expectedOwner, ok: true, stale: false, failedPaths: [] };
        },
      };
      sessionFilesHandleRef.current = handle;
      return () => {
        if (sessionFilesHandleRef.current === handle) sessionFilesHandleRef.current = null;
      };
    }, [currentFilesOwner, sessionFilesHandleRef]);

    useLayoutEffect(() => {
      if (!workspaceFilesHandleRef || !workspaceFileTarget) return undefined;
      const assignHandle = (handle: any) => {
        if (typeof workspaceFilesHandleRef === 'function') workspaceFilesHandleRef(handle);
        else workspaceFilesHandleRef.current = handle;
      };
      const handle = {
        owner: { scope: 'workspace', id: 'persistent', epoch: 0 },
        hasDirty: () => false,
        saveDirty: async () => ({ ok: true, failedEntryIds: [] }),
      };
      assignHandle(handle);
      return () => assignHandle(null);
    }, [workspaceFileTarget, workspaceFilesHandleRef]);

    useLayoutEffect(() => {
      onFilesFullChange?.(filesFull);
      return () => onFilesFullChange?.(false);
    }, [filesFull, onFilesFullChange]);

    useEffect(() => {
      const controller = new AbortController();
      mockControls.chatMountCount += 1;
      mockControls.chatController = controller;
      return () => {
        controller.abort();
        mockControls.chatAbortCount += 1;
      };
    }, []);

    return (
      <div
        data-testid="chat-v2"
        data-session-id={sessionId}
        data-selected-model-id={selectedModelId}
        data-active-slots={Array.from(activeSlotSessionIds ?? []).join(',')}
        data-workspace-entry-id={workspaceTargetResolving ? '' : workspaceFileTarget?.entry_id || ''}
        data-workspace-target-resolving={String(Boolean(workspaceTargetResolving))}
      >
        chat
        <label>
          草稿
          <input
            aria-label="ChatV2 草稿"
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
          />
        </label>
        <button type="button" onClick={() => setWaiting(true)}>mock-waiting</button>
        <span data-testid="chat-waiting">{waiting ? 'waiting_interaction' : 'idle'}</span>
        <button type="button" onClick={() => setFilesFull(true)}>mock-files-full</button>
        <button type="button" onClick={() => { if (sessionFilesHandleRef) sessionFilesHandleRef.current = null; }}>
          drop-files-handle
        </button>
        {filesFull && <div role="separator" aria-label="调整聊天和文件面板宽度" />}
        <button
          type="button"
          onClick={() => {
            void onCreateSession?.().then((createdSessionId) => {
              onSessionCreated?.(createdSessionId);
            });
          }}
        >create-chat-session</button>
        {workspaceFileTarget && (
          <button type="button" onClick={onWorkspaceFilesClose}>close-workspace-file</button>
        )}
      </div>
    );
  },
}));

vi.mock('../../components/SettingsCenter', () => ({
  default: ({
    onClose,
    onUnsavedChangesChange,
    permissionsRefreshToken,
  }: {
    onClose?: () => void;
    onUnsavedChangesChange?: (hasUnsavedChanges: boolean) => void;
    permissionsRefreshToken?: number;
  }) => (
    <div data-testid="settings-center-panel">
      <span data-testid="permissions-refresh-token">{permissionsRefreshToken}</span>
      <button onClick={() => onUnsavedChangesChange?.(true)}>mark-dirty</button>
      <button onClick={onClose}>close-config</button>
    </div>
  ),
}));

vi.mock('../../components/SkillsPage', () => ({
  default: () => <div data-testid="skills-page">skills-page</div>,
}));

vi.mock('../../components/ConnectionsPage', () => ({
  default: ({ active, onDirtyChange, onPermissionsInvalidated }: { active: boolean; onDirtyChange: (dirty: boolean) => void; onPermissionsInvalidated?: () => void }) => (
    <div data-testid="connections-page" data-active={String(active)}>
      connections-page
      <button onClick={() => onDirtyChange(true)}>mark-connection-dirty</button>
      <button onClick={onPermissionsInvalidated}>invalidate-mcp-permissions</button>
    </div>
  ),
}));

vi.mock('../../components/SchedulePage', () => ({
  default: () => <div data-testid="schedule-page">schedule-page</div>,
}));

describe('App 配置抽屉交互', () => {
  beforeAll(() => {
    vi.stubGlobal('Request', RouterTestRequest);
  });

  beforeEach(() => {
    mockControls.chatMountCount = 0;
    mockControls.chatAbortCount = 0;
    mockControls.chatController = null;
    mockControls.sessionFilesDirty = false;
    mockControls.sessionFilesSaveCalls = [];
    mockControls.sessionFilesSaveImpl = null;
    mockControls.workspaceGet.mockReset();
    mockControls.cronRunsGet.mockReset().mockResolvedValue({ runs: [], total: 0, offset: 0, limit: 20 });
  });

  afterEach(() => {
    vi.useRealTimers();
    mockControls.resolveRunningDuringManualSelect = null;
    window.history.replaceState({}, '', '/');
  });

  afterAll(() => {
    vi.stubGlobal('Request', NativeRequest);
  });

  it('关闭配置抽屉后应恢复左侧栏', async () => {
    render(<App />);

    expect(screen.getByTestId('sidebar-state')).toHaveTextContent('open');

    fireEvent.click(screen.getByText('open-config'));

    expect(screen.getByTestId('sidebar-state')).toHaveTextContent('open');
    expect(screen.getByTestId('settings-center-panel')).toBeInTheDocument();

    fireEvent.click(screen.getByText('close-config'));

    await waitFor(() => {
      expect(screen.getByTestId('sidebar-state')).toHaveTextContent('open');
    });
  });

  it('文件全屏时同一物理边界只保留会话分隔条', async () => {
    render(<App />);
    expect(screen.getByRole('separator', { name: '调整左侧栏宽度', hidden: true }))
      .toBeInTheDocument();
    expect(screen.getByRole('navigation', { name: '移动端主导航' })).toBeInTheDocument();

    fireEvent.click(screen.getByText('mock-files-full'));

    await waitFor(() => {
      expect(screen.queryByRole('navigation', { name: '移动端主导航' })).not.toBeInTheDocument();
      expect(screen.queryByRole('separator', { name: '调整左侧栏宽度', hidden: true }))
        .not.toBeInTheDocument();
      expect(screen.getAllByRole('separator', { hidden: true })).toHaveLength(1);
      expect(screen.getByRole('separator', { name: '调整聊天和文件面板宽度', hidden: true }))
        .toBeInTheDocument();
    });
  });

  it('设置中心打开时应使用 modal 语义并让背景不可聚焦', async () => {
    render(<App />);

    fireEvent.click(screen.getByText('open-config'));

    const dialog = screen.getByRole('dialog', { name: '设置' });
    expect(dialog).toHaveAttribute('aria-modal', 'true');
    await waitFor(() => {
      expect(screen.getByTestId('app-main-content')).toHaveAttribute('inert');
      expect(screen.getByTestId('app-main-content')).toHaveAttribute('aria-hidden', 'true');
      expect(document.activeElement).toBe(dialog);
    });
    fireEvent.keyDown(dialog, { key: 'Tab' });
    expect(dialog).toContainElement(document.activeElement as HTMLElement);

    fireEvent.click(screen.getByText('close-config'));

    await waitFor(() => {
      expect(screen.getByTestId('app-main-content')).not.toHaveAttribute('inert');
      expect(screen.getByTestId('app-main-content')).not.toHaveAttribute('aria-hidden');
    });
  });

  it('设置中心有未保存修改时关闭应先确认并支持取消', async () => {
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(false);

    render(<App />);

    fireEvent.click(screen.getByText('open-config'));
    fireEvent.click(screen.getByText('mark-dirty'));

    const modal = screen.getByTestId('settings-center-panel').parentElement?.parentElement;
    expect(modal).toHaveClass('opacity-100');

    fireEvent.click(screen.getByText('close-config'));

    expect(confirmSpy).not.toHaveBeenCalled();
    expect(screen.getByRole('alertdialog', { name: '放弃未保存的修改？' })).toBeInTheDocument();
    expect(modal).toHaveClass('opacity-100');

    fireEvent.click(screen.getByRole('button', { name: '继续编辑' }));
    await waitFor(() => {
      expect(screen.queryByRole('alertdialog', { name: '放弃未保存的修改？' })).not.toBeInTheDocument();
    });
    expect(modal).toHaveClass('opacity-100');

    fireEvent.click(screen.getByText('close-config'));
    fireEvent.click(screen.getByRole('button', { name: '放弃并关闭' }));

    await waitFor(() => {
      expect(modal).toHaveClass('opacity-0');
    });

    confirmSpy.mockRestore();
  });

  it('确认框不得拦截按钮自身的 Enter 语义', async () => {
    const user = userEvent.setup();
    render(<App />);

    fireEvent.click(screen.getByText('open-config'));
    fireEvent.click(screen.getByText('mark-dirty'));
    fireEvent.click(screen.getByText('close-config'));

    const continueButton = screen.getByRole('button', { name: '继续编辑' });
    continueButton.focus();
    await user.keyboard('{Enter}');

    await waitFor(() => {
      expect(screen.queryByRole('alertdialog', { name: '放弃未保存的修改？' })).not.toBeInTheDocument();
    });
    expect(screen.getByTestId('settings-center-panel').parentElement?.parentElement).toHaveClass('opacity-100');
  });

  it('设置中心有未保存修改时切换到日程一级页也应先确认', async () => {
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(false);

    render(<App />);

    fireEvent.click(screen.getByText('open-config'));
    fireEvent.click(screen.getByText('mark-dirty'));
    fireEvent.click(screen.getByText('open-cron'));

    expect(confirmSpy).not.toHaveBeenCalled();
    expect(screen.getByRole('alertdialog', { name: '放弃未保存的修改？' })).toBeInTheDocument();
    expect(screen.getByTestId('settings-center-panel')).toBeInTheDocument();
    expect(screen.queryByTestId('schedule-page')).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: '继续编辑' }));
    await waitFor(() => {
      expect(screen.queryByRole('alertdialog', { name: '放弃未保存的修改？' })).not.toBeInTheDocument();
    });
    expect(screen.queryByTestId('schedule-page')).not.toBeInTheDocument();

    fireEvent.click(screen.getByText('open-cron'));
    fireEvent.click(screen.getByRole('button', { name: '放弃并关闭' }));

    await waitFor(() => {
      expect(screen.getByTestId('schedule-page')).toBeInTheDocument();
      expect(screen.getByTestId('sidebar-state')).toHaveTextContent('open');
      expect(window.location.pathname).toBe('/schedule');
    });

    confirmSpy.mockRestore();
  });

  it('一级入口更新 URL，同时保持 ChatV2 原实例挂载', async () => {
    render(<App />);
    const originalChat = screen.getByTestId('chat-v2');

    fireEvent.click(screen.getByText('open-skills'));

    expect(window.location.pathname).toBe('/skills');
    expect(screen.getByTestId('active-primary-surface')).toHaveTextContent('skills');
    expect(screen.getByTestId('skills-page')).toBeInTheDocument();
    expect(screen.getByTestId('chat-primary-surface')).toHaveClass('hidden');
    expect(screen.getByTestId('chat-v2')).toBe(originalChat);

    fireEvent.click(screen.getByText('open-connections'));

    expect(window.location.pathname).toBe('/connections');
    expect(screen.getByTestId('connections-page')).toHaveAttribute('data-active', 'true');
    expect(screen.getByTestId('chat-v2')).toBe(originalChat);

    fireEvent.click(screen.getByText('open-cron'));

    expect(window.location.pathname).toBe('/schedule');
    expect(screen.getByTestId('schedule-page')).toBeInTheDocument();
    expect(screen.getByTestId('schedule-primary-surface')).not.toHaveClass('hidden');
    expect(screen.getByTestId('chat-v2')).toBe(originalChat);

    fireEvent.click(screen.getByText('sidebar-sessions'));
    await waitFor(() => expect(window.location.pathname).toBe('/'));
    expect(screen.getByTestId('sidebar-mode')).toHaveTextContent('sessions');
    expect(screen.getByTestId('active-primary-surface')).toHaveTextContent('chat');
    expect(screen.getByTestId('chat-v2')).toBe(originalChat);
  });

  it('工作区切换只投影左栏并在聊天右侧开文件，不创建一级 surface', async () => {
    render(<App />);
    const originalChat = screen.getByTestId('chat-v2');
    fireEvent.click(screen.getByText('sidebar-workspace'));
    await waitFor(() => expect(window.location.pathname).toBe('/workspace'));
    expect(screen.getByTestId('sidebar-mode')).toHaveTextContent('workspace');
    expect(screen.getByTestId('active-primary-surface')).toHaveTextContent('chat');
    expect(screen.queryByTestId('workspace-primary-surface')).not.toBeInTheDocument();
    expect(screen.getByTestId('chat-v2')).toBe(originalChat);

    fireEvent.click(screen.getByText('open-workspace-file'));
    await waitFor(() => expect(screen.getByTestId('chat-v2')).toHaveAttribute('data-workspace-entry-id', 'workspace-file'));
    expect(window.location.search).toContain('entry=workspace-file');

    fireEvent.click(screen.getByText('sidebar-sessions'));
    expect(window.location.pathname).toBe('/workspace');
    expect(window.location.search).toContain('entry=workspace-file');
    expect(screen.getByTestId('sidebar-mode')).toHaveTextContent('sessions');
    expect(screen.getByTestId('chat-v2')).toHaveAttribute('data-workspace-entry-id', 'workspace-file');

    fireEvent.click(screen.getByText('sidebar-workspace'));
    expect(screen.getByTestId('sidebar-mode')).toHaveTextContent('workspace');
    expect(window.location.pathname).toBe('/workspace');
    expect(screen.getByTestId('chat-v2')).toHaveAttribute('data-workspace-entry-id', 'workspace-file');
  });

  it('从首页点击工作区会持久化路由，重新挂载后首帧仍恢复工作区', async () => {
    window.history.replaceState({}, '', '/');
    const firstMount = render(<App />);

    fireEvent.click(screen.getByText('sidebar-workspace'));
    await waitFor(() => expect(window.location.pathname).toBe('/workspace'));
    expect(screen.getByTestId('sidebar-mode')).toHaveTextContent('workspace');

    firstMount.unmount();
    await act(async () => { await Promise.resolve(); });
    render(<App />);

    expect(screen.getByTestId('sidebar-mode')).toHaveTextContent('workspace');
    expect(screen.getByTestId('chat-primary-surface')).not.toHaveClass('hidden');
  });

  it('工作区顶部新建对话会在 flush 后回到会话路由', async () => {
    window.history.replaceState({}, '', '/workspace');
    render(<App />);
    expect(screen.getByTestId('sidebar-mode')).toHaveTextContent('workspace');

    fireEvent.click(screen.getByText('new-chat'));

    await waitFor(() => expect(window.location.pathname).toBe('/'));
    expect(screen.getByTestId('sidebar-mode')).toHaveTextContent('sessions');
    expect(screen.getByTestId('chat-v2')).toHaveAttribute('data-session-id', '');
  });

  it('移动端工作区 Sheet 选择会话后关闭 Sheet 并回到会话路由', async () => {
    window.history.replaceState({}, '', '/workspace');
    render(<App />);
    fireEvent.click(screen.getByRole('button', { name: '对话' }));
    const sheet = screen.getByRole('dialog', { name: '会话与工作区' });

    fireEvent.click(within(sheet).getByText('select-session-a'));

    await waitFor(() => expect(window.location.pathname).toBe('/'));
    expect(screen.queryByRole('dialog', { name: '会话与工作区' })).not.toBeInTheDocument();
    expect(screen.getByTestId('sidebar-mode')).toHaveTextContent('sessions');
    expect(screen.getByTestId('chat-v2')).toHaveAttribute('data-session-id', 'session-a');
  });

  it('移动端对话入口打开全屏会话/工作区 Sheet，并可从中打开文件', async () => {
    render(<App />);
    fireEvent.click(screen.getByRole('button', { name: '对话' }));
    const sheet = screen.getByRole('dialog', { name: '会话与工作区' });
    expect(screen.getAllByTestId('mobile-sidebar-mode')).toHaveLength(1);
    expect(screen.queryByTestId('sidebar-mode')).not.toBeInTheDocument();
    fireEvent.click(within(sheet).getByText('sidebar-workspace'));
    expect(within(sheet).getByTestId('mobile-sidebar-mode')).toHaveTextContent('workspace');
    fireEvent.click(within(sheet).getByText('open-workspace-file'));
    await waitFor(() => expect(screen.queryByRole('dialog', { name: '会话与工作区' })).not.toBeInTheDocument());
    expect(screen.getByTestId('chat-v2')).toHaveAttribute('data-workspace-entry-id', 'workspace-file');
  });

  it('/workspace?entry 深链映射到 chat + workspace sidebar + 右侧文件', async () => {
    window.history.replaceState({}, '', '/workspace?entry=deep-file');
    mockControls.workspaceGet.mockResolvedValueOnce({ data: { entry_id: 'deep-file', parent_id: null, name: 'deep.md', kind: 'file', path: 'deep.md', size_bytes: 1, mime_type: 'text/markdown', sha256: 'hash', revision: 1, status: 'active', created_at: 'now', updated_at: 'now' } });
    render(<App />);
    // 工作区模式必须由首帧 location 同步投影，不能等待 effect 或用户再次点击。
    expect(screen.getByTestId('sidebar-mode')).toHaveTextContent('workspace');
    await waitFor(() => expect(screen.getByTestId('chat-v2')).toHaveAttribute('data-workspace-entry-id', 'deep-file'));
    expect(screen.getByTestId('chat-primary-surface')).not.toHaveClass('hidden');
    expect(screen.queryByTestId('workspace-primary-surface')).not.toBeInTheDocument();
  });

  it('右侧 Workspace 文件打开后无需进入日程也立即轮询 Cron changes 并发出 invalidation', async () => {
    mockControls.cronRunsGet.mockResolvedValue({
      runs: [{
        id: 'global-cron-run',
        job_name: 'workspace-writer',
        cron_expr: '* * * * *',
        started_at: '2026-08-29T00:00:00Z',
        completed_at: '2026-08-29T00:00:01Z',
        status: 'success',
        output: null,
        is_read: false,
        artifacts: null,
        run_workspace: null,
        workspace_changes: [{
          entry_id: 'workspace-file',
          operation: 'updated',
          path: 'report.md',
          revision: 2,
        }],
      }],
      total: 1,
      offset: 0,
      limit: 20,
    });
    const invalidations: Array<{ entryId?: string; revision?: number }> = [];
    const unsubscribe = subscribeWorkspaceMutation((detail) => {
      if (detail.entryId === 'workspace-file') invalidations.push(detail);
    });
    render(<App />);
    await act(async () => { await Promise.resolve(); });
    expect(mockControls.cronRunsGet).not.toHaveBeenCalled();

    fireEvent.click(screen.getByText('open-workspace-file'));

    await waitFor(() => expect(mockControls.cronRunsGet).toHaveBeenCalledWith(undefined, 10, 0));
    expect(invalidations).toEqual([
      expect.objectContaining({ entryId: 'workspace-file', revision: 2, origin: 'server' }),
    ]);
    expect(screen.getByTestId('active-primary-surface')).toHaveTextContent('chat');
    unsubscribe();
  });

  it('首次深链解析失败时事务回滚到 /workspace，URL 不再指向未打开文件', async () => {
    window.history.replaceState({}, '', '/workspace?entry=missing-file&path=missing.md');
    mockControls.workspaceGet.mockRejectedValueOnce(new Error('missing'));

    render(<App />);

    expect(await screen.findByRole('alert')).toHaveTextContent('无法解析工作区深链');
    await waitFor(() => expect(`${window.location.pathname}${window.location.search}`).toBe('/workspace'));
    expect(screen.getByTestId('chat-v2')).toHaveAttribute('data-workspace-entry-id', '');
  });

  it('浏览器导航从 A 到 B 的延迟解析窗口隐藏 A，成功后才原子展示 B', async () => {
    const entryA = { entry_id: 'success-entry-a', parent_id: null, name: 'a.md', kind: 'file', path: 'a.md', size_bytes: 1, mime_type: 'text/markdown', sha256: 'hash-a', revision: 1, status: 'active', created_at: 'now', updated_at: 'now' };
    const entryB = { ...entryA, entry_id: 'success-entry-b', name: 'b.md', path: 'b.md', sha256: 'hash-b' };
    const entryBRequest = deferred<{ data: typeof entryB }>();
    window.history.replaceState({}, '', '/workspace?entry=success-entry-a&path=a.md');
    mockControls.workspaceGet
      .mockResolvedValueOnce({ data: entryA })
      .mockImplementationOnce(() => entryBRequest.promise);
    render(<App />);
    await waitFor(() => expect(screen.getByTestId('chat-v2')).toHaveAttribute('data-workspace-entry-id', 'success-entry-a'));

    act(() => {
      window.history.pushState({}, '', '/workspace?entry=success-entry-b&path=b.md');
      window.dispatchEvent(new PopStateEvent('popstate'));
    });

    expect(screen.getByTestId('chat-v2')).toHaveAttribute('data-workspace-entry-id', '');
    expect(screen.getByTestId('chat-v2')).toHaveAttribute('data-workspace-target-resolving', 'true');
    await act(async () => { entryBRequest.resolve({ data: entryB }); });
    await waitFor(() => expect(screen.getByTestId('chat-v2')).toHaveAttribute('data-workspace-entry-id', 'success-entry-b'));
    expect(screen.getByTestId('chat-v2')).toHaveAttribute('data-workspace-target-resolving', 'false');
  });

  it('从已提交文件 A 跳到失败深链 B 时 replace 回 A，面板与 URL 保持同一 entry', async () => {
    const entryA = { entry_id: 'entry-a', parent_id: null, name: 'a.md', kind: 'file', path: 'a.md', size_bytes: 1, mime_type: 'text/markdown', sha256: 'hash-a', revision: 1, status: 'active', created_at: 'now', updated_at: 'now' };
    const entryBRequest = deferred<{ data: never }>();
    window.history.replaceState({}, '', '/workspace?entry=entry-a&path=a.md');
    mockControls.workspaceGet
      .mockResolvedValueOnce({ data: entryA })
      .mockImplementationOnce(() => entryBRequest.promise);
    render(<App />);
    await waitFor(() => expect(screen.getByTestId('chat-v2')).toHaveAttribute('data-workspace-entry-id', 'entry-a'));

    act(() => {
      window.history.pushState({}, '', '/workspace?entry=entry-b&path=b.md');
      window.dispatchEvent(new PopStateEvent('popstate'));
    });

    expect(window.location.search).toContain('entry=entry-b');
    expect(screen.getByTestId('chat-v2')).toHaveAttribute('data-workspace-entry-id', '');
    expect(screen.getByTestId('chat-v2')).toHaveAttribute('data-workspace-target-resolving', 'true');
    entryBRequest.reject(new Error('entry-b missing'));
    expect(await screen.findByRole('alert')).toHaveTextContent('无法解析工作区深链');
    await waitFor(() => expect(`${window.location.pathname}${window.location.search}`).toBe('/workspace?entry=entry-a'));
    expect(screen.getByTestId('chat-v2')).toHaveAttribute('data-workspace-entry-id', 'entry-a');
  });

  it('工作区文件打开失败提示可由用户立即关闭', async () => {
    mockControls.workspaceGet.mockRejectedValueOnce(new Error('workspace unavailable'));
    render(<App />);

    act(() => {
      window.dispatchEvent(new CustomEvent('workspace:navigate', {
        detail: { entryId: 'missing-file' },
      }));
    });

    expect(await screen.findByRole('alert')).toHaveTextContent('无法打开工作区文件，请刷新工作区后重试。');
    fireEvent.click(screen.getByRole('button', { name: '关闭提示' }));
    expect(screen.queryByTestId('session-switch-save-error')).not.toBeInTheDocument();
  });

  it('关闭工作区文件时清除 entry 深链，不能被 route effect 立即重新打开', async () => {
    window.history.replaceState({}, '', '/workspace?entry=deep-file&path=deep.md');
    mockControls.workspaceGet.mockResolvedValueOnce({ data: { entry_id: 'deep-file', parent_id: null, name: 'deep.md', kind: 'file', path: 'deep.md', size_bytes: 1, mime_type: 'text/markdown', sha256: 'hash', revision: 1, status: 'active', created_at: 'now', updated_at: 'now' } });
    render(<App />);
    await waitFor(() => expect(screen.getByTestId('chat-v2')).toHaveAttribute('data-workspace-entry-id', 'deep-file'));

    fireEvent.click(screen.getByText('close-workspace-file'));

    await waitFor(() => expect(window.location.pathname).toBe('/workspace'));
    expect(window.location.search).toBe('');
    expect(screen.getByTestId('chat-v2')).toHaveAttribute('data-workspace-entry-id', '');
    await act(async () => { await Promise.resolve(); });
    expect(screen.getByTestId('chat-v2')).toHaveAttribute('data-workspace-entry-id', '');
  });

  it('批量删除命中当前文件时同步关闭标签并 replace 掉失效 entry 深链', async () => {
    window.history.replaceState({}, '', '/workspace?entry=deep-file&path=deep.md');
    mockControls.workspaceGet.mockResolvedValueOnce({ data: { entry_id: 'deep-file', parent_id: null, name: 'deep.md', kind: 'file', path: 'deep.md', size_bytes: 1, mime_type: 'text/markdown', sha256: 'hash', revision: 1, status: 'active', created_at: 'now', updated_at: 'now' } });
    render(<App />);
    await waitFor(() => expect(screen.getByTestId('chat-v2')).toHaveAttribute('data-workspace-entry-id', 'deep-file'));

    act(() => emitWorkspaceMutation({
      operation: 'delete',
      affectedEntryIds: ['deep-file', 'nested-file'],
      tombstone: true,
      origin: 'local',
    }));

    await waitFor(() => expect(`${window.location.pathname}${window.location.search}`).toBe('/workspace'));
    expect(screen.getByTestId('chat-v2')).toHaveAttribute('data-workspace-entry-id', '');
  });

  it('硬刷新 /workspace 时首帧同步进入工作区，不依赖 entry 请求', () => {
    window.history.replaceState({}, '', '/workspace');
    mockControls.workspaceGet.mockImplementation(() => new Promise(() => {}));

    render(<App />);

    expect(screen.getByTestId('sidebar-mode')).toHaveTextContent('workspace');
    expect(screen.getByTestId('active-primary-surface')).toHaveTextContent('chat');
    expect(screen.getByTestId('chat-primary-surface')).not.toHaveClass('hidden');
    expect(screen.getByTestId('chat-v2')).toBeInTheDocument();
  });

  it('切换一级入口时保留 ChatV2 草稿与 waiting 状态，且不重复挂载或 abort', async () => {
    const { unmount } = render(<App />);
    const originalChat = screen.getByTestId('chat-v2');
    const draft = screen.getByRole('textbox', { name: 'ChatV2 草稿' });
    fireEvent.change(draft, { target: { value: '这是未发送草稿' } });
    fireEvent.click(screen.getByRole('button', { name: 'mock-waiting' }));

    expect(mockControls.chatMountCount).toBe(1);
    expect(mockControls.chatController?.signal.aborted).toBe(false);
    expect(screen.getByTestId('chat-waiting')).toHaveTextContent('waiting_interaction');

    fireEvent.click(screen.getByText('open-skills'));
    fireEvent.click(screen.getByText('open-connections'));
    fireEvent.click(screen.getByText('open-cron'));

    expect(screen.getByTestId('chat-v2')).toBe(originalChat);
    expect(draft).toHaveValue('这是未发送草稿');
    expect(screen.getByTestId('chat-waiting')).toHaveTextContent('waiting_interaction');
    expect(mockControls.chatMountCount).toBe(1);
    expect(mockControls.chatAbortCount).toBe(0);
    expect(mockControls.chatController?.signal.aborted).toBe(false);

    unmount();
    expect(mockControls.chatAbortCount).toBe(1);
    expect(mockControls.chatController?.signal.aborted).toBe(true);
  });

  it('刷新进入 /skills 时恢复 Skills surface，聊天仍在后台挂载', async () => {
    window.history.replaceState({}, '', '/skills');
    render(<App />);

    expect(screen.getByTestId('active-primary-surface')).toHaveTextContent('skills');
    expect(screen.getByTestId('skills-page')).toBeInTheDocument();
    expect(screen.getByTestId('chat-primary-surface')).toHaveClass('hidden');
    expect(screen.getByTestId('chat-v2')).toBeInTheDocument();
  });

  it.each([
    ['/schedule/', 'schedule'],
    ['/skills/', 'skills'],
    ['/connections/', 'connections'],
  ])('刷新进入带尾斜杠的 %s 时恢复 %s surface', (pathname, surface) => {
    window.history.replaceState({}, '', pathname);
    render(<App />);

    expect(screen.getByTestId('active-primary-surface')).toHaveTextContent(surface);
    expect(screen.getByTestId(`${surface}-primary-surface`)).not.toHaveClass('hidden');
  });

  it('数据连接有未保存修改时阻止切换，确认放弃后才导航', async () => {
    render(<App />);
    fireEvent.click(screen.getByText('open-connections'));
    fireEvent.click(screen.getByText('mark-connection-dirty'));

    fireEvent.click(screen.getByText('open-skills'));
    expect(window.location.pathname).toBe('/connections');
    expect(screen.getByRole('alertdialog', { name: '放弃未保存的连接修改？' })).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: '继续编辑' }));
    expect(window.location.pathname).toBe('/connections');

    fireEvent.click(screen.getByText('open-skills'));
    fireEvent.click(screen.getByRole('button', { name: '放弃并切换' }));
    await waitFor(() => expect(window.location.pathname).toBe('/skills'));
    expect(screen.queryByRole('alertdialog', { name: '放弃未保存的连接修改？' })).not.toBeInTheDocument();
  });

  it('程序化切回对话时在确认前不执行会话选择，确认后只执行一次', async () => {
    render(<App />);
    fireEvent.click(screen.getByText('open-connections'));
    await waitFor(() => expect(window.location.pathname).toBe('/connections'));
    fireEvent.click(screen.getByText('mark-connection-dirty'));

    fireEvent.click(screen.getByText('select-manual'));
    expect(screen.getByRole('alertdialog', { name: '放弃未保存的连接修改？' })).toBeInTheDocument();
    expect(screen.getByTestId('chat-v2')).toHaveAttribute('data-session-id', '');
    fireEvent.click(screen.getByRole('button', { name: '继续编辑' }));
    expect(screen.getByTestId('chat-v2')).toHaveAttribute('data-session-id', '');

    fireEvent.click(screen.getByText('select-manual'));
    fireEvent.click(screen.getByRole('button', { name: '放弃并切换' }));
    await waitFor(() => {
      expect(window.location.pathname).toBe('/');
      expect(screen.getByTestId('chat-v2')).toHaveAttribute('data-session-id', 'manual-session');
    });
  });

  it('dirty A 切换到 B 时先保存，成功后才应用 B', async () => {
    render(<App />);
    fireEvent.click(screen.getByText('select-session-a'));
    await waitFor(() => expect(screen.getByTestId('chat-v2')).toHaveAttribute('data-session-id', 'session-a'));

    mockControls.sessionFilesDirty = true;
    fireEvent.click(screen.getByText('select-session-b'));

    await waitFor(() => expect(mockControls.sessionFilesSaveCalls).toHaveLength(1));
    expect(mockControls.sessionFilesSaveCalls[0]).toMatchObject({ ownerSessionId: 'session-a' });
    await waitFor(() => expect(screen.getByTestId('chat-v2')).toHaveAttribute('data-session-id', 'session-b'));
  });

  it('dirty A 的保存 Promise 未完成也立即切换到 B', async () => {
    let resolveSave!: (result: {
      ownerSessionId: string;
      ownerEpoch: number;
      ok: boolean;
      stale: boolean;
      failedPaths: string[];
    }) => void;
    render(<App />);
    fireEvent.click(screen.getByText('select-session-a'));
    await waitFor(() => expect(screen.getByTestId('chat-v2')).toHaveAttribute('data-session-id', 'session-a'));

    mockControls.sessionFilesDirty = true;
    mockControls.sessionFilesSaveImpl = (owner) => new Promise((resolve) => {
      resolveSave = (result) => {
        mockControls.sessionFilesDirty = false;
        resolve({ ...owner, ...result });
      };
    });
    fireEvent.click(screen.getByText('select-session-b'));

    await waitFor(() => expect(mockControls.sessionFilesSaveCalls).toHaveLength(1));
    expect(screen.getByTestId('chat-v2')).toHaveAttribute('data-session-id', 'session-b');

    await act(async () => {
      resolveSave({
        ownerSessionId: 'session-a',
        ownerEpoch: mockControls.sessionFilesSaveCalls[0].ownerEpoch,
        ok: true,
        stale: false,
        failedPaths: [],
      });
      await Promise.resolve();
    });
    expect(screen.queryByTestId('session-switch-save-error')).not.toBeInTheDocument();
  });

  it('dirty A 后台保存失败也切换到 B，返回 A 时聊天草稿仍在', async () => {
    render(<App />);
    fireEvent.click(screen.getByText('select-session-a'));
    await waitFor(() => expect(screen.getByTestId('chat-v2')).toHaveAttribute('data-session-id', 'session-a'));
    const draft = screen.getByRole('textbox', { name: 'ChatV2 草稿' });
    fireEvent.change(draft, { target: { value: 'A 的未保存草稿' } });

    mockControls.sessionFilesDirty = true;
    mockControls.sessionFilesSaveImpl = async (owner) => ({
      ...owner,
      ok: false,
      stale: false,
      failedPaths: ['report.md'],
    });
    fireEvent.click(screen.getByText('select-session-b'));

    await waitFor(() => expect(screen.getByTestId('chat-v2')).toHaveAttribute('data-session-id', 'session-b'));
    expect(screen.queryByTestId('session-switch-save-error')).not.toBeInTheDocument();
    fireEvent.click(screen.getByText('select-session-a'));
    await waitFor(() => expect(screen.getByRole('textbox', { name: 'ChatV2 草稿' })).toHaveValue('A 的未保存草稿'));
  });

  it('dirty Session 下的 entry 深链立即打开，文件保存留在后台', async () => {
    render(<App />);
    fireEvent.click(screen.getByText('select-session-a'));
    await waitFor(() => expect(screen.getByTestId('chat-v2')).toHaveAttribute('data-session-id', 'session-a'));

    mockControls.sessionFilesDirty = true;
    mockControls.sessionFilesSaveImpl = async (owner) => ({
      ...owner,
      ok: false,
      stale: false,
      failedPaths: ['report.md'],
    });
    mockControls.workspaceGet.mockResolvedValue({ data: { entry_id: 'deep-file', parent_id: null, name: 'deep.md', kind: 'file', path: 'deep.md', size_bytes: 1, mime_type: 'text/markdown', sha256: 'hash', revision: 1, status: 'active', created_at: 'now', updated_at: 'now' } });

    // 地址栏/前进后退进入深链，绕过了应用内的点击入口。
    act(() => {
      window.history.pushState({}, '', '/workspace?entry=deep-file');
      window.dispatchEvent(new PopStateEvent('popstate'));
    });

    await waitFor(() => expect(screen.getByTestId('chat-v2')).toHaveAttribute('data-workspace-entry-id', 'deep-file'));
    expect(window.location.pathname).toBe('/workspace');
    expect(mockControls.workspaceGet).toHaveBeenCalled();
    expect(mockControls.sessionFilesSaveCalls).toHaveLength(1);
    expect(screen.queryByTestId('session-switch-save-error')).not.toBeInTheDocument();
  });

  it('dirty Session 下的 entry 深链 flush 成功后才投影目标文件', async () => {
    render(<App />);
    fireEvent.click(screen.getByText('select-session-a'));
    await waitFor(() => expect(screen.getByTestId('chat-v2')).toHaveAttribute('data-session-id', 'session-a'));

    mockControls.sessionFilesDirty = true;
    mockControls.sessionFilesSaveImpl = async (owner) => ({
      ...owner,
      ok: true,
      stale: false,
      failedPaths: [],
    });
    mockControls.workspaceGet.mockResolvedValue({ data: { entry_id: 'deep-file', parent_id: null, name: 'deep.md', kind: 'file', path: 'deep.md', size_bytes: 1, mime_type: 'text/markdown', sha256: 'hash', revision: 1, status: 'active', created_at: 'now', updated_at: 'now' } });

    act(() => {
      window.history.pushState({}, '', '/workspace?entry=deep-file');
      window.dispatchEvent(new PopStateEvent('popstate'));
    });

    await waitFor(() => expect(screen.getByTestId('chat-v2')).toHaveAttribute('data-workspace-entry-id', 'deep-file'));
    expect(mockControls.sessionFilesSaveCalls).toHaveLength(1);
    expect(window.location.pathname).toBe('/workspace');
  });

  it('当前 Session 的文件 owner handle 暂时缺失时也立即切换', async () => {
    render(<App />);
    fireEvent.click(screen.getByText('select-session-a'));
    await waitFor(() => expect(screen.getByTestId('chat-v2')).toHaveAttribute('data-session-id', 'session-a'));

    fireEvent.click(screen.getByText('drop-files-handle'));
    fireEvent.click(screen.getByText('select-session-b'));

    await waitFor(() => expect(screen.getByTestId('chat-v2')).toHaveAttribute('data-session-id', 'session-b'));
    expect(screen.queryByTestId('session-switch-save-error')).not.toBeInTheDocument();
  });

  it('dirty A 新建会话前同样先保存，成功后才回到欢迎页', async () => {
    render(<App />);
    fireEvent.click(screen.getByText('select-session-a'));
    await waitFor(() => expect(screen.getByTestId('chat-v2')).toHaveAttribute('data-session-id', 'session-a'));

    mockControls.sessionFilesDirty = true;
    fireEvent.click(screen.getByText('new-chat'));

    await waitFor(() => expect(mockControls.sessionFilesSaveCalls).toHaveLength(1));
    expect(mockControls.sessionFilesSaveCalls[0]).toMatchObject({ ownerSessionId: 'session-a' });
    await waitFor(() => expect(screen.getByTestId('chat-v2')).toHaveAttribute('data-session-id', ''));
  });

  it('数据连接未保存时 Back 与 Forward 都留在当前页，确认后只重放一次', async () => {
    const historyGoSpy = vi.spyOn(window.history, 'go');
    render(<App />);

    fireEvent.click(screen.getByText('open-connections'));
    await waitFor(() => expect(window.location.pathname).toBe('/connections'));
    fireEvent.click(screen.getByText('open-skills'));
    await waitFor(() => expect(window.location.pathname).toBe('/skills'));

    act(() => window.history.back());
    await waitFor(() => expect(window.location.pathname).toBe('/connections'));
    fireEvent.click(screen.getByText('mark-connection-dirty'));

    act(() => window.history.forward());
    expect(await screen.findByRole('alertdialog', { name: '放弃未保存的连接修改？' })).toBeInTheDocument();
    await waitFor(() => expect(window.location.pathname).toBe('/connections'));
    fireEvent.click(screen.getByRole('button', { name: '继续编辑' }));
    expect(window.location.pathname).toBe('/connections');

    act(() => window.history.back());
    expect(await screen.findByRole('alertdialog', { name: '放弃未保存的连接修改？' })).toBeInTheDocument();
    await waitFor(() => expect(window.location.pathname).toBe('/connections'));
    fireEvent.click(screen.getByRole('button', { name: '继续编辑' }));
    expect(window.location.pathname).toBe('/connections');

    const forwardReplayCount = historyGoSpy.mock.calls.filter(([delta]) => delta === 1).length;
    act(() => window.history.forward());
    expect(await screen.findByRole('alertdialog', { name: '放弃未保存的连接修改？' })).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '放弃并切换' }));

    await waitFor(() => expect(window.location.pathname).toBe('/skills'));
    expect(historyGoSpy.mock.calls.filter(([delta]) => delta === 1)).toHaveLength(forwardReplayCount + 1);
    historyGoSpy.mockRestore();
  });

  it('数据连接的 MCP 变更会刷新设置中的权限策略版本', async () => {
    render(<App />);
    fireEvent.click(screen.getByText('open-connections'));
    fireEvent.click(screen.getByText('invalidate-mcp-permissions'));
    fireEvent.click(screen.getByText('invalidate-mcp-permissions'));
    fireEvent.click(screen.getByText('open-config'));

    expect(screen.getByTestId('permissions-refresh-token')).toHaveTextContent('2');
  });

  it('模型列表较晚返回时不应覆盖已恢复的会话模型', async () => {
    let resolveModels!: (value: {
      models: Array<{
        id: string;
        name: string;
        provider: string;
        supports_thinking: boolean;
        supports_image: boolean;
        max_images: number;
        supports_video: boolean;
        max_videos: number;
        max_tokens: number;
        enabled: boolean;
        tags: string[];
      }>;
      default_model: string;
      subagent_default_model: string;
    }) => void;
    vi.mocked(apiService.getModels).mockReturnValueOnce(new Promise((resolve) => {
      resolveModels = resolve;
    }));

    render(<App />);

    fireEvent.click(screen.getByText('select-qwen-model'));
    expect(screen.getByTestId('chat-v2')).toHaveAttribute('data-selected-model-id', 'qwen-plus');

    await act(async () => {
      resolveModels({
        models: [
          {
            id: 'glm-default',
            name: 'GLM Default',
            provider: 'test',
            supports_thinking: false,
            supports_image: false,
            max_images: 0,
            supports_video: false,
            max_videos: 0,
            max_tokens: 8192,
            enabled: true,
            tags: [],
          },
          {
            id: 'qwen-plus',
            name: 'Qwen Plus',
            provider: 'test',
            supports_thinking: true,
            supports_image: true,
            max_images: 5,
            supports_video: false,
            max_videos: 0,
            max_tokens: 8192,
            enabled: true,
            tags: [],
          },
        ],
        default_model: 'glm-default',
        subagent_default_model: 'glm-default',
      });
    });

    expect(screen.getByTestId('chat-v2')).toHaveAttribute('data-selected-model-id', 'qwen-plus');
  });

  it('挂载后应立即同步 running-sessions 并选中第一个运行中会话', async () => {
    vi.mocked(apiService.getRunningSessions).mockResolvedValueOnce({
      running_sessions: [
        { session_id: 'session-a', round_id: null },
        { session_id: 'session-b', round_id: 'round-b' },
      ],
    });

    render(<App />);

    await waitFor(() => {
      expect(screen.getByTestId('executing-sessions')).toHaveTextContent('session-b');
    });

    expect(screen.getByTestId('chat-v2')).toHaveAttribute('data-active-slots', 'session-a,session-b');
    expect(screen.getByTestId('chat-v2')).toHaveAttribute('data-session-id', 'session-a');
  });

  it('初始 running-sessions 返回前手动选择会话时不应被自动切换覆盖', async () => {
    let resolveInitialRunning!: () => void;
    const initialRunning = new Promise<{ running_sessions: { session_id: string; round_id: string | null }[] }>((resolve) => {
      resolveInitialRunning = () => resolve({
        running_sessions: [
          { session_id: 'session-a', round_id: null },
        ],
      });
    });
    vi.mocked(apiService.getRunningSessions).mockReturnValueOnce(initialRunning);
    mockControls.resolveRunningDuringManualSelect = resolveInitialRunning;

    render(<App />);

    await waitFor(() => {
      expect(apiService.getRunningSessions).toHaveBeenCalledTimes(1);
    });

    fireEvent.click(screen.getByText('select-manual'));

    await waitFor(() => {
      expect(screen.getByTestId('executing-sessions')).toBeEmptyDOMElement();
    });

    expect(screen.getByTestId('chat-v2')).toHaveAttribute('data-active-slots', 'session-a');
    expect(screen.getByTestId('chat-v2')).toHaveAttribute('data-session-id', 'manual-session');
  });

  it('首次同步为空后后续 running-sessions 只同步标记不自动切换', async () => {
    vi.useFakeTimers();
    vi.mocked(apiService.getRunningSessions)
      .mockResolvedValueOnce({ running_sessions: [] })
      .mockResolvedValueOnce({
        running_sessions: [
          { session_id: 'session-a', round_id: 'round-a' },
        ],
      });

    render(<App />);

    await act(async () => {
      await Promise.resolve();
    });

    expect(screen.getByTestId('chat-v2')).toHaveAttribute('data-session-id', '');

    await act(async () => {
      await vi.advanceTimersByTimeAsync(5000);
    });

    expect(screen.getByTestId('executing-sessions')).toHaveTextContent('session-a');
    expect(screen.getByTestId('chat-v2')).toHaveAttribute('data-active-slots', 'session-a');
    expect(screen.getByTestId('chat-v2')).toHaveAttribute('data-session-id', '');
  });

  it('应周期同步 running-sessions 并清理后台完成的执行标记', async () => {
    vi.useFakeTimers();
    vi.mocked(apiService.getRunningSessions)
      .mockResolvedValueOnce({
        running_sessions: [
          { session_id: 'session-a', round_id: null },
          { session_id: 'session-b', round_id: 'round-b' },
        ],
      })
      .mockResolvedValueOnce({
        running_sessions: [
          { session_id: 'session-b', round_id: 'round-b' },
        ],
      });

    render(<App />);

    await act(async () => {
      await Promise.resolve();
    });

    expect(screen.getByTestId('executing-sessions')).toHaveTextContent('session-b');
    expect(screen.getByTestId('chat-v2')).toHaveAttribute('data-active-slots', 'session-a,session-b');

    await act(async () => {
      await vi.advanceTimersByTimeAsync(5000);
    });

    expect(screen.getByTestId('executing-sessions')).toHaveTextContent('session-b');
    expect(screen.getByTestId('executing-sessions')).not.toHaveTextContent('session-a');
    expect(screen.getByTestId('chat-v2')).toHaveAttribute('data-active-slots', 'session-b');
  });

  it('迟到的隐式建会话响应不应覆盖用户手动选择的会话', async () => {
    let resolveCreateSession!: (response: { session_id: string; message: string }) => void;
    vi.mocked(apiService.createSession).mockImplementationOnce(() => new Promise((resolve) => {
      resolveCreateSession = resolve;
    }));

    render(<App />);
    fireEvent.click(screen.getByText('create-chat-session'));
    expect(apiService.createSession).toHaveBeenCalledTimes(1);

    fireEvent.click(screen.getByText('select-manual'));
    expect(screen.getByTestId('chat-v2')).toHaveAttribute('data-session-id', 'manual-session');

    await act(async () => {
      resolveCreateSession({ session_id: 'late-created-session', message: 'created' });
    });

    await waitFor(() => {
      expect(screen.getByTestId('chat-v2')).toHaveAttribute('data-session-id', 'manual-session');
    });
  });
});
