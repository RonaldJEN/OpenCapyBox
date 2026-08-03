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
    isAdminUser: vi.fn(() => false),
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

    fireEvent.click(screen.getByRole('button', { name: '打开会话 測試會話 1' }));
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

    fireEvent.click(screen.getByRole('button', { name: '打开会话 測試會話 1' }));
    expect(mockOnSelect).toHaveBeenCalledWith('session-1', { roundId: 'round-hit-1' });
  });

  it('当前会话来自刷新恢复时应同步会话模型', async () => {
    const mockOnModelChange = vi.fn();
    vi.mocked(apiService.getSessions).mockResolvedValue({
      sessions: [{
        ...mockSessions[0],
        model_id: 'qwen-plus',
      }],
    });

    render(
      <SessionList
        currentSessionId="session-1"
        onSessionSelect={vi.fn()}
        onModelChange={mockOnModelChange}
      />,
    );

    await waitFor(() => {
      expect(mockOnModelChange).toHaveBeenCalledWith('qwen-plus');
    });
  });

  it('點擊登出應該調用 logout 並導航', async () => {
    render(<SessionList onSessionSelect={vi.fn()} />);

    await waitFor(() => {
      expect(screen.getByLabelText('账户菜单')).toBeInTheDocument();
    });

    fireEvent.click(screen.getByLabelText('账户菜单'));
    fireEvent.click(screen.getByText('退出登录'));

    expect(apiService.logout).toHaveBeenCalled();
    expect(mockNavigate).toHaveBeenCalledWith('/login');
  });

  it('管理员账户菜单应提供管理后台入口', async () => {
    vi.mocked(apiService.isAdminUser).mockReturnValue(true);
    render(<SessionList onSessionSelect={vi.fn()} />);

    await waitFor(() => {
      expect(screen.getByLabelText('账户菜单')).toBeInTheDocument();
    });

    fireEvent.click(screen.getByLabelText('账户菜单'));
    fireEvent.click(screen.getByText('管理后台'));

    expect(mockNavigate).toHaveBeenCalledWith('/admin');
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

  it('首次挂载时不应独立请求运行中会话', async () => {
    render(<SessionList onSessionSelect={vi.fn()} />);

    await waitFor(() => {
      expect(screen.getByText('OpenCapyBox')).toBeInTheDocument();
    });

    expect(apiService.getRunningSessions).not.toHaveBeenCalled();
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

    expect(screen.getByLabelText('搜索会话')).toHaveFocus();

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

  it('删除按钮应有可访问名称，并打开应用内确认弹窗而不选中会话', async () => {
    const mockOnSelect = vi.fn();
    const confirmSpy = vi.spyOn(window, 'confirm');
    render(<SessionList onSessionSelect={mockOnSelect} />);

    const deleteButton = await screen.findByRole('button', { name: '删除会话 測試會話 1' });
    expect(deleteButton).toHaveAttribute('type', 'button');
    expect(deleteButton).toHaveAttribute('title', '删除会话 測試會話 1');

    fireEvent.click(deleteButton);

    const dialog = screen.getByRole('alertdialog', { name: '删除会话“測試會話 1”？' });
    expect(dialog).toHaveAttribute('aria-modal', 'true');
    expect(dialog).toHaveAccessibleDescription('删除后无法恢复。');
    expect(screen.getByText('删除后无法恢复。')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '取消' })).toHaveFocus();
    expect(mockOnSelect).not.toHaveBeenCalled();
    expect(confirmSpy).not.toHaveBeenCalled();
    confirmSpy.mockRestore();
  });

  it('删除确认弹窗应锁定焦点，并在 Escape 取消后恢复删除按钮焦点', async () => {
    render(<SessionList onSessionSelect={vi.fn()} />);

    const deleteButton = await screen.findByRole('button', { name: '删除会话 測試會話 1' });
    fireEvent.click(deleteButton);

    const dialog = screen.getByRole('alertdialog');
    const cancelButton = screen.getByRole('button', { name: '取消' });
    const confirmButton = screen.getByRole('button', { name: '确认删除' });

    confirmButton.focus();
    fireEvent.keyDown(confirmButton, { key: 'Tab' });
    expect(cancelButton).toHaveFocus();

    fireEvent.keyDown(cancelButton, { key: 'Tab', shiftKey: true });
    expect(confirmButton).toHaveFocus();

    fireEvent.keyDown(dialog, { key: 'Escape' });
    await waitFor(() => {
      expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument();
      expect(deleteButton).toHaveFocus();
    });
  });

  it('点击遮罩应取消删除并恢复原删除按钮焦点', async () => {
    render(<SessionList onSessionSelect={vi.fn()} />);

    const deleteButton = await screen.findByRole('button', { name: '删除会话 測試會話 1' });
    fireEvent.click(deleteButton);
    const dialog = screen.getByRole('alertdialog');
    const overlay = dialog.parentElement;
    expect(overlay).not.toBeNull();

    fireEvent.click(overlay!);
    await waitFor(() => {
      expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument();
      expect(deleteButton).toHaveFocus();
    });
  });

  it('取消时目标行已因刷新消失，应聚焦相邻可用会话', async () => {
    vi.mocked(apiService.getSessions)
      .mockResolvedValueOnce({ sessions: mockSessions })
      .mockResolvedValueOnce({ sessions: [mockSessions[1]] });
    const { rerender } = render(
      <SessionList refreshTrigger={0} onSessionSelect={vi.fn()} onNewChat={vi.fn()} />,
    );

    fireEvent.click(await screen.findByRole('button', { name: '删除会话 測試會話 1' }));
    expect(screen.getByRole('alertdialog')).toBeInTheDocument();

    rerender(
      <SessionList refreshTrigger={1} onSessionSelect={vi.fn()} onNewChat={vi.fn()} />,
    );
    await waitFor(() => {
      expect(screen.queryByRole('button', { name: '打开会话 測試會話 1' })).not.toBeInTheDocument();
      expect(screen.getByRole('button', { name: '打开会话 測試會話 2' })).toBeInTheDocument();
    });

    fireEvent.click(screen.getByRole('button', { name: '取消' }));
    await waitFor(() => {
      expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument();
      expect(screen.getByRole('button', { name: '打开会话 測試會話 2' })).toHaveFocus();
    });
  });

  it('删除进行中应锁定弹窗并阻止重复提交，成功后聚焦下一条会话', async () => {
    let resolveDelete!: () => void;
    vi.mocked(apiService.deleteSession).mockImplementationOnce(
      () => new Promise<void>((resolve) => {
        resolveDelete = resolve;
      }),
    );
    const mockOnSelect = vi.fn();
    render(
      <SessionList
        currentSessionId="session-2"
        onSessionSelect={mockOnSelect}
        onNewChat={vi.fn()}
      />,
    );

    fireEvent.click(await screen.findByRole('button', { name: '删除会话 測試會話 1' }));
    fireEvent.click(screen.getByRole('button', { name: '确认删除' }));

    const dialog = screen.getByRole('alertdialog');
    await waitFor(() => {
      expect(dialog).toHaveAttribute('aria-busy', 'true');
      expect(screen.getByRole('button', { name: '删除中…' })).toBeDisabled();
      expect(screen.getByRole('button', { name: '取消' })).toBeDisabled();
    });

    fireEvent.keyDown(dialog, { key: 'Escape' });
    fireEvent.click(dialog.parentElement!);
    fireEvent.click(screen.getByRole('button', { name: '删除中…' }));
    expect(screen.getByRole('alertdialog')).toBeInTheDocument();
    expect(apiService.deleteSession).toHaveBeenCalledTimes(1);

    await act(async () => {
      resolveDelete();
    });

    await waitFor(() => {
      expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument();
      expect(screen.queryByText('測試會話 1')).not.toBeInTheDocument();
      expect(screen.getByRole('button', { name: '打开会话 測試會話 2' })).toHaveFocus();
    });
    expect(mockOnSelect).not.toHaveBeenCalled();
  });

  it('删除失败应显示可重试错误，关闭后恢复原删除按钮焦点', async () => {
    const consoleErrorSpy = vi.spyOn(console, 'error').mockImplementation(() => {});
    vi.mocked(apiService.deleteSession).mockRejectedValueOnce(new Error('network error'));
    render(<SessionList onSessionSelect={vi.fn()} />);

    const deleteButton = await screen.findByRole('button', { name: '删除会话 測試會話 1' });
    fireEvent.click(deleteButton);
    fireEvent.click(screen.getByRole('button', { name: '确认删除' }));

    expect(await screen.findByRole('alert')).toHaveTextContent('删除失败，请重试。');
    expect(screen.getByRole('button', { name: '重试删除' })).toHaveFocus();
    expect(screen.getByRole('alertdialog')).toHaveAttribute('aria-busy', 'false');

    fireEvent.click(screen.getByRole('button', { name: '取消' }));
    await waitFor(() => {
      expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument();
      expect(deleteButton).toHaveFocus();
    });
    consoleErrorSpy.mockRestore();
  });

  it('失败后确认按钮应可重试删除', async () => {
    const consoleErrorSpy = vi.spyOn(console, 'error').mockImplementation(() => {});
    vi.mocked(apiService.deleteSession)
      .mockRejectedValueOnce(new Error('temporary error'))
      .mockResolvedValueOnce();
    render(<SessionList onSessionSelect={vi.fn()} onNewChat={vi.fn()} />);

    fireEvent.click(await screen.findByRole('button', { name: '删除会话 測試會話 1' }));
    fireEvent.click(screen.getByRole('button', { name: '确认删除' }));
    fireEvent.click(await screen.findByRole('button', { name: '重试删除' }));

    await waitFor(() => {
      expect(apiService.deleteSession).toHaveBeenCalledTimes(2);
      expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument();
    });
    consoleErrorSpy.mockRestore();
  });

  it('删除当前会话成功后应返回欢迎页', async () => {
    vi.mocked(apiService.deleteSession).mockResolvedValueOnce();
    const mockOnSelect = vi.fn();
    render(
      <SessionList
        currentSessionId="session-1"
        onSessionSelect={mockOnSelect}
        onNewChat={vi.fn()}
      />,
    );

    fireEvent.click(await screen.findByRole('button', { name: '删除会话 測試會話 1' }));
    fireEvent.click(screen.getByRole('button', { name: '确认删除' }));

    await waitFor(() => {
      expect(mockOnSelect).toHaveBeenCalledWith('');
      expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument();
    });
  });

  it('删除列表末项后应聚焦上一条会话', async () => {
    vi.mocked(apiService.deleteSession).mockResolvedValueOnce();
    render(<SessionList onSessionSelect={vi.fn()} onNewChat={vi.fn()} />);

    fireEvent.click(await screen.findByRole('button', { name: '删除会话 測試會話 2' }));
    fireEvent.click(screen.getByRole('button', { name: '确认删除' }));

    await waitFor(() => {
      expect(screen.getByRole('button', { name: '打开会话 測試會話 1' })).toHaveFocus();
    });
  });

  it('删除唯一会话后应聚焦新建对话按钮', async () => {
    vi.mocked(apiService.getSessions).mockResolvedValue({ sessions: [mockSessions[0]] });
    vi.mocked(apiService.deleteSession).mockResolvedValueOnce();
    render(<SessionList onSessionSelect={vi.fn()} onNewChat={vi.fn()} />);

    fireEvent.click(await screen.findByRole('button', { name: '删除会话 測試會話 1' }));
    fireEvent.click(screen.getByRole('button', { name: '确认删除' }));

    await waitFor(() => {
      expect(screen.getByRole('button', { name: '新建对话' })).toHaveFocus();
    });
  });

  it('删除接口成功后即使列表刷新挂起也应立即关闭弹窗并恢复焦点', async () => {
    let getSessionsCalls = 0;
    vi.mocked(apiService.getSessions).mockImplementation(() => {
      getSessionsCalls += 1;
      // 首次加载正常返回，后续刷新永久挂起以模拟刷新阻塞。
      if (getSessionsCalls === 1) return Promise.resolve({ sessions: mockSessions });
      return new Promise(() => {});
    });
    vi.mocked(apiService.deleteSession).mockResolvedValueOnce();
    render(<SessionList onSessionSelect={vi.fn()} onNewChat={vi.fn()} />);

    fireEvent.click(await screen.findByRole('button', { name: '删除会话 測試會話 1' }));
    fireEvent.click(screen.getByRole('button', { name: '确认删除' }));

    // 刷新挂起不得阻塞弹窗关闭与本地移除。
    await waitFor(() => {
      expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument();
      expect(screen.queryByText('測試會話 1')).not.toBeInTheDocument();
      expect(screen.getByRole('button', { name: '打开会话 測試會話 2' })).toHaveFocus();
    });
  });

  it('删除成功后迟到的陈旧刷新不得把已删除行重新加回', async () => {
    let resolveRefresh: ((value: { sessions: typeof mockSessions }) => void) | undefined;
    let getSessionsCalls = 0;
    vi.mocked(apiService.getSessions).mockImplementation(() => {
      getSessionsCalls += 1;
      if (getSessionsCalls === 1) return Promise.resolve({ sessions: mockSessions });
      return new Promise((resolve) => {
        resolveRefresh = resolve;
      });
    });
    vi.mocked(apiService.deleteSession).mockResolvedValueOnce();
    render(<SessionList onSessionSelect={vi.fn()} onNewChat={vi.fn()} />);

    fireEvent.click(await screen.findByRole('button', { name: '删除会话 測試會話 1' }));
    fireEvent.click(screen.getByRole('button', { name: '确认删除' }));

    await waitFor(() => {
      expect(screen.queryByText('測試會話 1')).not.toBeInTheDocument();
      expect(resolveRefresh).toBeDefined();
    });

    // 迟到的刷新返回仍含已删除会话的陈旧列表，守卫必须过滤掉它。
    await act(async () => {
      resolveRefresh!({ sessions: mockSessions });
    });

    await waitFor(() => {
      expect(screen.queryByText('測試會話 1')).not.toBeInTheDocument();
      expect(screen.getByText('測試會話 2')).toBeInTheDocument();
    });
  });
});
