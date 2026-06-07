import { afterEach, describe, it, expect, vi } from 'vitest';
import { act, render, screen, fireEvent, waitFor } from '@testing-library/react';
import App from '../../App';
import { apiService } from '../../services/api';

const mockControls = vi.hoisted(() => ({
  resolveRunningDuringManualSelect: null as (() => void) | null,
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
  SessionList: ({ isCollapsed, onOpenConfig, onSessionSelect, executingSessionIds }: { isCollapsed?: boolean; onOpenConfig?: () => void; onSessionSelect?: (sessionId: string) => void; executingSessionIds?: Set<string> }) => (
    <div>
      <div data-testid="sidebar-state">{isCollapsed ? 'collapsed' : 'open'}</div>
      <div data-testid="executing-sessions">{Array.from(executingSessionIds ?? []).join(',')}</div>
      <button onClick={onOpenConfig}>open-config</button>
      <button onClick={() => {
        onSessionSelect?.('manual-session');
        mockControls.resolveRunningDuringManualSelect?.();
      }}>select-manual</button>
    </div>
  ),
}));

vi.mock('../../components/ChatV2', () => ({
  ChatV2: ({ sessionId, activeSlotSessionIds }: { sessionId?: string; activeSlotSessionIds?: Set<string> }) => (
    <div
      data-testid="chat-v2"
      data-session-id={sessionId}
      data-active-slots={Array.from(activeSlotSessionIds ?? []).join(',')}
    >chat</div>
  ),
}));

vi.mock('../../components/AgentConfig', () => ({
  default: ({ onClose }: { onClose?: () => void }) => (
    <div data-testid="agent-config-panel">
      <button onClick={onClose}>close-config</button>
    </div>
  ),
}));

vi.mock('../../components/SkillManager', () => ({
  default: () => <div data-testid="skills-panel">skills</div>,
}));

vi.mock('../../components/CronSchedule', () => ({
  default: () => <div data-testid="cron-panel">cron</div>,
}));

describe('App 配置抽屉交互', () => {
  afterEach(() => {
    vi.useRealTimers();
    mockControls.resolveRunningDuringManualSelect = null;
  });

  it('关闭配置抽屉后应恢复左侧栏', async () => {
    render(<App />);

    expect(screen.getByTestId('sidebar-state')).toHaveTextContent('open');

    fireEvent.click(screen.getByText('open-config'));

    expect(screen.getByTestId('sidebar-state')).toHaveTextContent('collapsed');
    expect(screen.getByTestId('agent-config-panel')).toBeInTheDocument();

    fireEvent.click(screen.getByText('close-config'));

    await waitFor(() => {
      expect(screen.getByTestId('sidebar-state')).toHaveTextContent('open');
    });
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
      expect(screen.getByTestId('executing-sessions')).toHaveTextContent('session-a,session-b');
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
      expect(screen.getByTestId('executing-sessions')).toHaveTextContent('session-a');
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

    expect(screen.getByTestId('executing-sessions')).toHaveTextContent('session-a,session-b');
    expect(screen.getByTestId('chat-v2')).toHaveAttribute('data-active-slots', 'session-a,session-b');

    await act(async () => {
      await vi.advanceTimersByTimeAsync(5000);
    });

    expect(screen.getByTestId('executing-sessions')).toHaveTextContent('session-b');
    expect(screen.getByTestId('executing-sessions')).not.toHaveTextContent('session-a');
    expect(screen.getByTestId('chat-v2')).toHaveAttribute('data-active-slots', 'session-b');
  });
});
