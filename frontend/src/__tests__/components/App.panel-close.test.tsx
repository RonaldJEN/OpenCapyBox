import { afterEach, describe, it, expect, vi } from 'vitest';
import { act, render, screen, fireEvent, waitFor } from '@testing-library/react';
import App from '../../App';
import { apiService } from '../../services/api';

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
  SessionList: ({ isCollapsed, onOpenConfig, executingSessionIds }: { isCollapsed?: boolean; onOpenConfig?: () => void; executingSessionIds?: Set<string> }) => (
    <div>
      <div data-testid="sidebar-state">{isCollapsed ? 'collapsed' : 'open'}</div>
      <div data-testid="executing-sessions">{Array.from(executingSessionIds ?? []).join(',')}</div>
      <button onClick={onOpenConfig}>open-config</button>
    </div>
  ),
}));

vi.mock('../../components/ChatV2', () => ({
  ChatV2: ({ activeSlotSessionIds }: { activeSlotSessionIds?: Set<string> }) => (
    <div data-testid="chat-v2" data-active-slots={Array.from(activeSlotSessionIds ?? []).join(',')}>chat</div>
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
      await vi.advanceTimersByTimeAsync(5000);
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
