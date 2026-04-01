import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import App from '../../App';

vi.mock('../../services/api', () => ({
  apiService: {
    getUserId: vi.fn(() => 'demo-user'),
    getModels: vi.fn().mockResolvedValue({
      models: [{ id: 'test-model', name: 'Test Model' }],
      default_model: 'test-model',
    }),
    createSession: vi.fn().mockResolvedValue({
      session_id: 'new-session',
    }),
  },
}));

vi.mock('../../components/SessionList', () => ({
  SessionList: ({ isCollapsed, onOpenConfig }: { isCollapsed?: boolean; onOpenConfig?: () => void }) => (
    <div>
      <div data-testid="sidebar-state">{isCollapsed ? 'collapsed' : 'open'}</div>
      <button onClick={onOpenConfig}>open-config</button>
    </div>
  ),
}));

vi.mock('../../components/ChatV2', () => ({
  ChatV2: () => <div data-testid="chat-v2">chat</div>,
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

vi.mock('../../components/CronHistory', () => ({
  default: () => <div data-testid="cron-panel">cron</div>,
}));

describe('App 配置抽屉交互', () => {
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
});
