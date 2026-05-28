import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor, act } from '../utils/test-utils';
import { SessionList } from '../../components/SessionList';
import { apiService } from '../../services/api';
import { SessionStatus } from '../../types';

// Mock apiService
vi.mock('../../services/api', () => ({
  apiService: {
    getSessions: vi.fn(),
    deleteSession: vi.fn(),
    logout: vi.fn(),
    getUserId: vi.fn(() => 'mock-session'),
    getRunningSessions: vi.fn().mockResolvedValue({ running_sessions: [] }),
  },
}));

// Mock useNavigate
const mockNavigate = vi.fn();
vi.mock('react-router-dom', async () => {
  const actual = await vi.importActual('react-router-dom');
  return {
    ...actual,
    useNavigate: () => mockNavigate,
  };
});

describe('SessionList 組件', () => {
  const mockSessions = [
    {
      id: 'session-1',
      user_id: 'user-1',
      status: SessionStatus.ACTIVE,
      created_at: new Date().toISOString(),
      updated_at: new Date().toISOString(),
      title: '測試會話 1',
    },
    {
      id: 'session-2',
      user_id: 'user-1',
      status: SessionStatus.COMPLETED,
      created_at: new Date().toISOString(),
      updated_at: new Date().toISOString(),
      title: '測試會話 2',
    },
  ];

  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(apiService.getSessions).mockResolvedValue({
      sessions: mockSessions,
    });
    vi.mocked(apiService.getRunningSessions).mockResolvedValue({ running_sessions: [] });
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('應該顯示載入狀態', () => {
    vi.mocked(apiService.getSessions).mockImplementation(
      () => new Promise(() => {})
    );

    render(<SessionList onSessionSelect={vi.fn()} />);

    // 檢查是否有載入動畫
    expect(document.querySelector('.animate-spin')).toBeInTheDocument();
  });

  it('應該顯示會話列表', async () => {
    render(<SessionList onSessionSelect={vi.fn()} />);

    await waitFor(() => {
      expect(screen.getByText('測試會話 1')).toBeInTheDocument();
      expect(screen.getByText('測試會話 2')).toBeInTheDocument();
    });
  });

  it('點擊會話應該觸發 onSessionSelect', async () => {
    const mockOnSelect = vi.fn();
    render(<SessionList onSessionSelect={mockOnSelect} />);

    await waitFor(() => {
      expect(screen.getByText('測試會話 1')).toBeInTheDocument();
    });

    fireEvent.click(screen.getByText('測試會話 1'));
    expect(mockOnSelect).toHaveBeenCalledWith('session-1');
  });

  it('点击搜索命中的会话时应传递 round 定位目标', async () => {
    const mockOnSelect = vi.fn();
    vi.mocked(apiService.getSessions).mockResolvedValue({
      sessions: [{
        ...mockSessions[0],
        match_type: 'assistant',
        match_excerpt: '包含 用户画像 的回复摘要',
        match_round_id: 'round-hit-1',
      }],
    });

    render(<SessionList onSessionSelect={mockOnSelect} />);

    await waitFor(() => {
      expect(screen.getByText('測試會話 1')).toBeInTheDocument();
    });

    fireEvent.click(screen.getByText('測試會話 1'));
    expect(mockOnSelect).toHaveBeenCalledWith('session-1', { roundId: 'round-hit-1' });
  });

  it('點擊登出應該調用 logout 並導航', async () => {
    render(<SessionList onSessionSelect={vi.fn()} />);

    await waitFor(() => {
      expect(screen.getByText('退出登录')).toBeInTheDocument();
    });

    fireEvent.click(screen.getByText('退出登录'));

    expect(apiService.logout).toHaveBeenCalled();
    expect(mockNavigate).toHaveBeenCalledWith('/login');
  });

  it('應該顯示品牌名稱 OpenCapyBox', async () => {
    render(<SessionList onSessionSelect={vi.fn()} />);

    await waitFor(() => {
      expect(screen.getByText('OpenCapyBox')).toBeInTheDocument();
    });
  });

  it('應該顯示 HISTORY 標籤', async () => {
    render(<SessionList onSessionSelect={vi.fn()} />);

    await waitFor(() => {
      expect(screen.getByText('History')).toBeInTheDocument();
    });
  });

  it('應該顯示新建對話按鈕並觸發 onNewChat', async () => {
    const mockOnNewChat = vi.fn();
    render(<SessionList onSessionSelect={vi.fn()} onNewChat={mockOnNewChat} />);

    await waitFor(() => {
      expect(screen.getByTitle('新建对话')).toBeInTheDocument();
    });

    fireEvent.click(screen.getByTitle('新建对话'));
    expect(mockOnNewChat).toHaveBeenCalledTimes(1);
  });

  it('未傳入 onNewChat 時不應顯示新建對話按鈕', async () => {
    render(<SessionList onSessionSelect={vi.fn()} />);

    await waitFor(() => {
      expect(screen.getByText('OpenCapyBox')).toBeInTheDocument();
    });

    expect(screen.queryByTitle('新建对话')).not.toBeInTheDocument();
  });

  it('cronUnreadCount 大于 0 时显示日程未读数字徽标', async () => {
    render(<SessionList onSessionSelect={vi.fn()} cronUnreadCount={2} />);

    await waitFor(() => {
      expect(screen.getByText('日程管理')).toBeInTheDocument();
    });

    expect(screen.getByText('2')).toBeInTheDocument();
  });

  it('首次挂载时应上报所有运行中会话', async () => {
    const mockOnRunningSessionsDetected = vi.fn();
    vi.mocked(apiService.getRunningSessions).mockResolvedValue({
      running_sessions: [
        { session_id: 'session-1', round_id: 'round-1' },
        { session_id: 'session-2', round_id: null },
      ],
    });

    render(
      <SessionList
        onSessionSelect={vi.fn()}
        onRunningSessionsDetected={mockOnRunningSessionsDetected}
      />
    );

    await waitFor(() => {
      expect(mockOnRunningSessionsDetected).toHaveBeenCalledWith([
        { session_id: 'session-1', round_id: 'round-1' },
        { session_id: 'session-2', round_id: null },
      ]);
    });
  });

  it('应为多个运行中会话同时显示执行标记', async () => {
    render(
      <SessionList
        onSessionSelect={vi.fn()}
        executingSessionIds={new Set(['session-1', 'session-2'])}
      />
    );

    await waitFor(() => {
      expect(screen.getByText('測試會話 1')).toBeInTheDocument();
      expect(screen.getByText('測試會話 2')).toBeInTheDocument();
    });

    expect(document.querySelectorAll('.animate-dot-pulse')).toHaveLength(2);
  });

  it('输入搜索词后应调用带 q 的 getSessions', async () => {
    render(<SessionList onSessionSelect={vi.fn()} />);

    await waitFor(() => {
      expect(screen.getByText('測試會話 1')).toBeInTheDocument();
    });

    fireEvent.change(screen.getByLabelText('搜索会话'), {
      target: { value: '測試' },
    });

    await waitFor(() => {
      expect(apiService.getSessions).toHaveBeenCalledWith('測試');
    });
  });

  it('清空搜索后应恢复完整列表请求', async () => {
    render(<SessionList onSessionSelect={vi.fn()} />);

    await waitFor(() => {
      expect(screen.getByText('測試會話 1')).toBeInTheDocument();
    });

    fireEvent.change(screen.getByLabelText('搜索会话'), {
      target: { value: '測試' },
    });

    await waitFor(() => {
      expect(apiService.getSessions).toHaveBeenCalledWith('測試');
    });

    const callsBeforeClear = vi.mocked(apiService.getSessions).mock.calls.length;
    fireEvent.click(screen.getByLabelText('清空搜索'));

    await waitFor(() => {
      expect(vi.mocked(apiService.getSessions).mock.calls.length).toBeGreaterThan(callsBeforeClear);
    });
    const calls = vi.mocked(apiService.getSessions).mock.calls;
    expect(calls[calls.length - 1]).toEqual([]);
  });

  it('消息命中时应显示摘要', async () => {
    vi.mocked(apiService.getSessions).mockResolvedValue({
      sessions: [{
        ...mockSessions[0],
        match_type: 'assistant',
        match_excerpt: '这里是包含 搜索 关键词的消息摘要',
      }],
    });

    render(<SessionList onSessionSelect={vi.fn()} />);

    await waitFor(() => {
      expect(screen.getByText('这里是包含 搜索 关键词的消息摘要')).toBeInTheDocument();
    });
    expect(screen.getByText('Agent 回复:')).toBeInTheDocument();
  });

  it('搜索无结果时应显示专用空态', async () => {
    vi.mocked(apiService.getSessions)
      .mockResolvedValueOnce({ sessions: mockSessions })
      .mockResolvedValueOnce({ sessions: [] });

    render(<SessionList onSessionSelect={vi.fn()} />);

    await waitFor(() => {
      expect(screen.getByText('測試會話 1')).toBeInTheDocument();
    });

    fireEvent.change(screen.getByLabelText('搜索会话'), {
      target: { value: '不存在' },
    });

    await waitFor(() => {
      expect(screen.getByText('没有匹配的对话')).toBeInTheDocument();
    });
  });

  it('旧搜索响应晚返回时不应覆盖新结果', async () => {
    let resolveOldSearch!: (value: { sessions: typeof mockSessions }) => void;
    let resolveNewSearch!: (value: { sessions: typeof mockSessions }) => void;
    const oldSearchPromise = new Promise<{ sessions: typeof mockSessions }>((resolve) => {
      resolveOldSearch = resolve;
    });
    const newSearchPromise = new Promise<{ sessions: typeof mockSessions }>((resolve) => {
      resolveNewSearch = resolve;
    });

    vi.mocked(apiService.getSessions)
      .mockResolvedValueOnce({ sessions: mockSessions })
      .mockImplementationOnce(() => oldSearchPromise)
      .mockImplementationOnce(() => newSearchPromise);

    render(<SessionList onSessionSelect={vi.fn()} />);

    await waitFor(() => {
      expect(screen.getByText('測試會話 1')).toBeInTheDocument();
    });

    fireEvent.change(screen.getByLabelText('搜索会话'), {
      target: { value: '旧' },
    });
    await waitFor(() => {
      expect(apiService.getSessions).toHaveBeenCalledWith('旧');
    });

    fireEvent.change(screen.getByLabelText('搜索会话'), {
      target: { value: '新' },
    });
    await waitFor(() => {
      expect(apiService.getSessions).toHaveBeenCalledWith('新');
    });

    await act(async () => {
      resolveNewSearch({
        sessions: [{
          ...mockSessions[0],
          id: 'new-result',
          title: '新结果',
        }],
      });
    });

    await waitFor(() => {
      expect(screen.getByText('新结果')).toBeInTheDocument();
    });

    await act(async () => {
      resolveOldSearch({
        sessions: [{
          ...mockSessions[0],
          id: 'old-result',
          title: '旧结果',
        }],
      });
    });

    expect(screen.getByText('新结果')).toBeInTheDocument();
    expect(screen.queryByText('旧结果')).not.toBeInTheDocument();
  });
});
