import { useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
import { afterAll, afterEach, beforeAll, beforeEach, describe, it, expect, vi } from 'vitest';
import { act, render, screen, fireEvent, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import App from '../../App';
import { apiService } from '../../services/api';

const NativeRequest = globalThis.Request;

class RouterTestRequest extends NativeRequest {
  constructor(input: RequestInfo | URL, init?: RequestInit) {
    // jsdom and Node's undici expose distinct AbortSignal implementations.
    // These routes have no loaders, so keep the native Request signal while
    // still exercising the real data-router PUSH/POP blocker state machine.
    super(input, init ? { ...init, signal: undefined } : init);
  }
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
}));

vi.mock('../../services/api', () => ({
  apiService: {
    isAuthenticated: vi.fn(() => true),
    getUserId: vi.fn(() => 'demo-user'),
    isAdminUser: vi.fn(() => false),
    getAxiosClient: vi.fn(() => ({
      get: vi.fn(),
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
}));

vi.mock('../../components/SessionList', () => ({
  SessionList: ({ isCollapsed, onOpenConfig, onOpenCron, onOpenSkills, onOpenConnections, activePrimarySurface, onSessionSelect, onModelChange, onNewChat, executingSessionIds }: { isCollapsed?: boolean; onOpenConfig?: () => void; onOpenCron?: () => void; onOpenSkills?: () => void; onOpenConnections?: () => void; activePrimarySurface?: string; onSessionSelect?: (sessionId: string) => void; onModelChange?: (modelId: string) => void; onNewChat?: () => void; executingSessionIds?: Set<string> }) => (
    <div>
      <div data-testid="sidebar-state">{isCollapsed ? 'collapsed' : 'open'}</div>
      <div data-testid="executing-sessions">{Array.from(executingSessionIds ?? []).join(',')}</div>
      <div data-testid="active-primary-surface">{activePrimarySurface}</div>
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
  }: {
    sessionId?: string;
    selectedModelId?: string;
    activeSlotSessionIds?: Set<string>;
    onCreateSession?: (modelId?: string) => Promise<string>;
    onSessionCreated?: (sessionId: string) => void;
    onFilesFullChange?: (full: boolean) => void;
    sessionFilesHandleRef?: { current: any };
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

    fireEvent.click(screen.getByText('mock-files-full'));

    await waitFor(() => {
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

  it('dirty A 的保存 Promise resolve 前必须一直停留在 A', async () => {
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
    expect(screen.getByTestId('chat-v2')).toHaveAttribute('data-session-id', 'session-a');

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
    await waitFor(() => expect(screen.getByTestId('chat-v2')).toHaveAttribute('data-session-id', 'session-b'));
  });

  it('dirty A 保存失败时留在 A 并保留当前草稿', async () => {
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

    expect(await screen.findByTestId('session-switch-save-error')).toHaveTextContent('文件保存失败');
    expect(screen.getByTestId('chat-v2')).toHaveAttribute('data-session-id', 'session-a');
    expect(draft).toHaveValue('A 的未保存草稿');
  });

  it('当前 Session 的文件 owner handle 暂时缺失时保守阻止切换', async () => {
    render(<App />);
    fireEvent.click(screen.getByText('select-session-a'));
    await waitFor(() => expect(screen.getByTestId('chat-v2')).toHaveAttribute('data-session-id', 'session-a'));

    fireEvent.click(screen.getByText('drop-files-handle'));
    fireEvent.click(screen.getByText('select-session-b'));

    expect(await screen.findByTestId('session-switch-save-error')).toHaveTextContent('文件编辑状态仍在同步');
    expect(screen.getByTestId('chat-v2')).toHaveAttribute('data-session-id', 'session-a');
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
