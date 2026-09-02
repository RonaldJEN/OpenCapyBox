import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor, act, within } from '../utils/test-utils';
import { SessionList } from '../../components/SessionList';
import { apiService } from '../../services/api';
import { SessionStatus } from '../../types';

const workspaceClient = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn(), put: vi.fn(), patch: vi.fn(), delete: vi.fn() }));

// Mock apiService
vi.mock('../../services/api', () => ({
  apiService: {
    getSessions: vi.fn(),
    deleteSession: vi.fn(),
    logout: vi.fn(),
    getUserId: vi.fn(() => 'mock-session'),
    isAdminUser: vi.fn(() => false),
    getRunningSessions: vi.fn().mockResolvedValue({ running_sessions: [] }),
    getAxiosClient: vi.fn(() => workspaceClient),
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
    workspaceClient.get.mockResolvedValue({ data: { items: [], next_cursor: null, workspace_revision: 1 } });
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('会话加载使用紧凑骨架，不显示会无限旋转的 spinner', () => {
    vi.mocked(apiService.getSessions).mockImplementation(
      () => new Promise(() => {})
    );

    render(<SessionList onSessionSelect={vi.fn()} />);

    expect(screen.getByTestId('session-loading-state')).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: '会话' })).toBeInTheDocument();
    expect(screen.getByRole('status', { name: '正在加载会话' })).toBeInTheDocument();
    expect(screen.queryByText('正在加载会话…')).not.toBeInTheDocument();
    expect(screen.getByTestId('session-loading-state').querySelector('.animate-spin')).toBeNull();
  });

  it('会话请求永久 pending 时不得阻塞工作区首屏投影', () => {
    vi.mocked(apiService.getSessions).mockImplementation(() => new Promise(() => {}));
    workspaceClient.get.mockImplementation(() => new Promise(() => {}));

    render(<SessionList sidebarMode="workspace" onSessionSelect={vi.fn()} />);

    expect(screen.getByRole('tab', { name: '工作区' })).toHaveAttribute('aria-selected', 'true');
    expect(screen.getByTestId('workspace-sidebar-content')).toBeInTheDocument();
    expect(screen.queryByTestId('session-loading-state')).not.toBeInTheDocument();
  });

  it('会话请求失败后结束 loading，并允许显式重试', async () => {
    vi.mocked(apiService.getSessions)
      .mockRejectedValueOnce(new Error('network unavailable'))
      .mockResolvedValueOnce({ sessions: mockSessions });

    render(<SessionList onSessionSelect={vi.fn()} />);

    expect(await screen.findByTestId('session-load-error')).toHaveTextContent('会话加载失败，请重试。');
    expect(screen.queryByTestId('session-loading-state')).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '重试' }));
    expect(screen.getByTestId('session-loading-state')).toBeInTheDocument();
    expect(await screen.findByText('測試會話 1')).toBeInTheDocument();
    expect(apiService.getSessions).toHaveBeenCalledTimes(2);
  });

  it('應該顯示會話列表', async () => {
    render(<SessionList onSessionSelect={vi.fn()} />);

    await waitFor(() => {
      expect(screen.getByText('測試會話 1')).toBeInTheDocument();
      expect(screen.getByText('測試會話 2')).toBeInTheDocument();
    });
  });

  it('会话列表移除 HISTORY，并以固定紧凑行承载标题与次级信息', async () => {
    render(<SessionList currentSessionId="session-1" onSessionSelect={vi.fn()} />);

    await screen.findByText('測試會話 1');
    expect(screen.queryByText('History', { exact: true })).not.toBeInTheDocument();
    expect(screen.getByRole('tabpanel', { name: '会话' })).toHaveClass('flex', 'min-h-0', 'flex-1', 'pt-2');
    expect(screen.getByTestId('session-list-scroll')).toHaveClass('min-h-0', 'flex-1', 'space-y-1', 'overflow-y-auto');
    expect(screen.getByTestId('session-row-session-1')).toHaveClass('h-12', 'border-claude-border', 'bg-white/90');
    expect(screen.getByTestId('session-row-session-2')).toHaveClass('h-12', 'border-transparent');
  });

  it('会话空态占满列表剩余区域并居中显示操作指引', async () => {
    vi.mocked(apiService.getSessions).mockResolvedValue({ sessions: [] });
    render(<SessionList onSessionSelect={vi.fn()} />);

    expect(await screen.findByText('暂无对话记录')).toBeInTheDocument();
    expect(screen.getByTestId('session-empty-state')).toHaveClass(
      'flex', 'min-h-0', 'flex-1', 'items-center', 'justify-center', 'text-center',
    );
    expect(screen.getByText('新建对话后，会话会保存在这里')).toBeInTheDocument();
  });

  it('折叠状态只由外层 AppSidebar 控制，不在列表内重复做宽度动画', async () => {
    render(<SessionList isCollapsed onSessionSelect={vi.fn()} />);
    await screen.findByText('測試會話 1');

    const sidebar = screen.getByRole('complementary');
    expect(sidebar).toHaveClass('w-full', 'p-4');
    expect(sidebar).not.toHaveClass('w-0', 'opacity-0', 'transition-[width,opacity,padding,border-color]');
  });

  it('创建成功的会话应立即插入本地列表且不触发全量刷新', async () => {
    const { rerender } = render(<SessionList onSessionSelect={vi.fn()} />);

    await screen.findByText('測試會話 1');
    vi.mocked(apiService.getSessions).mockClear();

    const now = new Date().toISOString();
    rerender(
      <SessionList
        onSessionSelect={vi.fn()}
        optimisticSession={{
          id: 'session-new',
          user_id: 'user-1',
          status: SessionStatus.ACTIVE,
          created_at: now,
          updated_at: now,
          title: '新会话',
          model_id: 'qwen-plus',
        }}
      />,
    );

    expect(await screen.findByText('新会话')).toBeInTheDocument();
    expect(apiService.getSessions).not.toHaveBeenCalled();
  });

  it('切换当前会话应复用本地列表而不是重新请求', async () => {
    const { rerender } = render(
      <SessionList currentSessionId="session-1" onSessionSelect={vi.fn()} />,
    );

    await screen.findByText('測試會話 1');
    vi.mocked(apiService.getSessions).mockClear();

    rerender(<SessionList currentSessionId="session-2" onSessionSelect={vi.fn()} />);

    expect(screen.getByText('測試會話 2')).toBeInTheDocument();
    expect(apiService.getSessions).not.toHaveBeenCalled();
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

  it('應該顯示品牌名稱 bsbox', async () => {
    render(<SessionList onSessionSelect={vi.fn()} />);

    await waitFor(() => {
      expect(screen.getByText('bsbox')).toBeInTheDocument();
    });
  });

  it('應該顯示新建對話按鈕並觸發 onNewChat', async () => {
    const mockOnNewChat = vi.fn();
    render(<SessionList onSessionSelect={vi.fn()} onNewChat={mockOnNewChat} />);

    await waitFor(() => {
      expect(screen.getAllByTitle('新建对话')).toHaveLength(2);
    });

    fireEvent.click(screen.getAllByTitle('新建对话')[0]);
    expect(mockOnNewChat).toHaveBeenCalledTimes(1);
  });

  it('未傳入 onNewChat 時不應顯示新建對話按鈕', async () => {
    render(<SessionList onSessionSelect={vi.fn()} />);

    await waitFor(() => {
      expect(screen.getByText('bsbox')).toBeInTheDocument();
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

  it('提供日程、Skills 与数据一级入口、右侧提示并标记当前页面', async () => {
    const onOpenCron = vi.fn();
    const onOpenSkills = vi.fn();
    const onOpenConnections = vi.fn();
    render(
      <SessionList
        onSessionSelect={vi.fn()}
        activePrimarySurface="schedule"
        onOpenCron={onOpenCron}
        onOpenSkills={onOpenSkills}
        onOpenConnections={onOpenConnections}
      />,
    );

    await screen.findByText('測試會話 1');
    const scheduleButton = screen.getByRole('button', { name: '日程管理' });
    const skillsButton = screen.getByRole('button', { name: 'Skills' });
    const dataButton = screen.getByRole('button', { name: '数据' });
    expect(scheduleButton).toHaveAttribute('aria-current', 'page');
    expect(skillsButton).not.toHaveAttribute('aria-current');
    expect(dataButton).not.toHaveAttribute('aria-current');
    expect(scheduleButton).toHaveAccessibleDescription('安排自动任务');
    expect(skillsButton).toHaveAccessibleDescription('复用优质经验');
    expect(dataButton).toHaveAccessibleDescription('连接内外部数据');
    expect(screen.getByText('安排自动任务')).toHaveClass('truncate');
    expect(screen.getByText('复用优质经验')).toHaveClass('truncate');
    expect(screen.getByText('连接内外部数据')).toHaveClass('truncate');
    expect(screen.queryByText('数据连接')).not.toBeInTheDocument();

    fireEvent.click(scheduleButton);
    fireEvent.click(skillsButton);
    fireEvent.click(dataButton);
    expect(onOpenCron).toHaveBeenCalledTimes(1);
    expect(onOpenSkills).toHaveBeenCalledTimes(1);
    expect(onOpenConnections).toHaveBeenCalledTimes(1);
  });

  it('会话行只在对话 surface 中声明为当前页', async () => {
    const { rerender } = render(
      <SessionList
        currentSessionId="session-1"
        activePrimarySurface="skills"
        onSessionSelect={vi.fn()}
      />,
    );

    const sessionButton = await screen.findByRole('button', { name: '打开会话 測試會話 1' });
    expect(sessionButton).not.toHaveAttribute('aria-current');

    rerender(
      <SessionList
        currentSessionId="session-1"
        activePrimarySurface="chat"
        onSessionSelect={vi.fn()}
      />,
    );
    expect(sessionButton).toHaveAttribute('aria-current', 'page');
  });

  it('首次挂载时不应独立请求运行中会话', async () => {
    render(<SessionList onSessionSelect={vi.fn()} />);

    await waitFor(() => {
      expect(screen.getByText('bsbox')).toBeInTheDocument();
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
    expect(screen.getByText('測試會話 1')).toBeInTheDocument();

    await waitFor(() => {
      expect(vi.mocked(apiService.getSessions).mock.calls.length).toBeGreaterThan(callsBeforeClear);
    });
    const calls = vi.mocked(apiService.getSessions).mock.calls;
    expect(calls[calls.length - 1]).toEqual([]);
  });

  it('搜索请求尚未返回时仍可清空，并立即恢复完整列表缓存', async () => {
    let resolveSearch!: (value: { sessions: typeof mockSessions }) => void;
    vi.mocked(apiService.getSessions)
      .mockResolvedValueOnce({ sessions: mockSessions })
      .mockImplementationOnce(() => new Promise((resolve) => { resolveSearch = resolve; }));
    render(<SessionList onSessionSelect={vi.fn()} />);
    expect(await screen.findByText('測試會話 1')).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText('搜索会话'), { target: { value: '仍在搜索' } });
    expect(screen.getByRole('status', { name: '正在搜索会话' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '清空搜索' })).toBeInTheDocument();
    await waitFor(() => expect(apiService.getSessions).toHaveBeenCalledWith('仍在搜索'));
    fireEvent.click(screen.getByRole('button', { name: '清空搜索' }));

    expect(screen.getByText('測試會話 1')).toBeInTheDocument();
    expect(screen.queryByRole('status', { name: '正在搜索会话' })).not.toBeInTheDocument();
    await act(async () => {
      resolveSearch({ sessions: [] });
      await Promise.resolve();
    });
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
      expect(screen.getAllByRole('button', { name: '新建对话' })[0]).toHaveFocus();
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

  it('会话/工作区使用 ARIA tabs，支持方向键切换并保持可见焦点', async () => {
    const onModeChange = vi.fn();
    const { rerender } = render(<SessionList onSessionSelect={vi.fn()} sidebarMode="sessions" onSidebarModeChange={onModeChange} />);
    await screen.findByRole('tab', { name: '会话' });
    const searchSlotClass = screen.getByTestId('sidebar-search-slot').className;
    const tabsClass = screen.getByTestId('sidebar-mode-tabs').className;
    expect(screen.getByTestId('sidebar-search-control')).toHaveClass('h-9');
    const sessionsTab = screen.getByRole('tab', { name: '会话' });
    expect(sessionsTab).toHaveAttribute('aria-selected', 'true');
    fireEvent.keyDown(sessionsTab, { key: 'ArrowRight' });
    expect(onModeChange).toHaveBeenCalledWith('workspace');
    expect(screen.getByRole('tab', { name: '工作区' })).toHaveFocus();

    rerender(<SessionList onSessionSelect={vi.fn()} sidebarMode="workspace" onSidebarModeChange={onModeChange} />);
    expect(await screen.findByTestId('workspace-sidebar-content')).toBeInTheDocument();
    expect(document.getElementById('sidebar-workspace-panel')).toHaveClass('-mx-3');
    expect(screen.getByRole('tab', { name: '工作区' })).toHaveAttribute('aria-selected', 'true');
    expect(screen.getByTestId('sidebar-search-slot')).toHaveClass(...searchSlotClass.split(' '));
    expect(screen.getByTestId('sidebar-mode-tabs')).toHaveClass(...tabsClass.split(' '));
    expect(screen.getByRole('textbox', { name: '搜索会话' })).toHaveClass('h-9');
    expect(screen.getByRole('textbox', { name: '搜索会话' })).toHaveAttribute('placeholder', '搜索对话');
    expect(screen.getByTestId('sidebar-mode-tabs')).not.toHaveClass('rounded-xl', 'bg-claude-hover/70');
    expect(screen.getByTestId('workspace-panel-header')).toHaveClass('absolute', '-top-11');
    expect(screen.getByRole('button', { name: '搜索工作区文件' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '工作区操作' })).toBeInTheDocument();
  });

  it('浏览会话列表后返回工作区时保留展开状态并刷新根目录与可见子目录', async () => {
    const folder = {
      entry_id: 'folder-1', parent_id: null, name: '研究', kind: 'directory', path: '研究',
      size_bytes: 0, mime_type: null, sha256: null, revision: 1, status: 'active',
      created_at: 'now', updated_at: 'now',
    };
    const child = {
      entry_id: 'file-1', parent_id: 'folder-1', name: '报告.md', kind: 'file', path: '研究/报告.md',
      size_bytes: 12, mime_type: 'text/markdown', sha256: 'hash', revision: 1, status: 'active',
      created_at: 'now', updated_at: 'now',
    };
    const refreshedChild = {
      ...child,
      entry_id: 'file-2',
      name: '最新报告.md',
      path: '研究/最新报告.md',
      revision: 2,
    };
    let childRequestCount = 0;
    workspaceClient.get.mockImplementation(async (_url, config) => ({
      data: {
        items: config?.params?.parent_id === 'folder-1'
          ? [childRequestCount++ === 0 ? child : refreshedChild]
          : [folder],
        next_cursor: null,
        workspace_revision: 1,
      },
    }));

    const { rerender } = render(
      <SessionList onSessionSelect={vi.fn()} sidebarMode="workspace" />,
    );
    fireEvent.click(await screen.findByRole('button', { name: '展开 研究' }));
    expect(await screen.findByRole('button', { name: '报告.md' })).toBeInTheDocument();
    fireEvent.click(screen.getByRole('checkbox', { name: '选择 报告.md' }));

    rerender(<SessionList onSessionSelect={vi.fn()} sidebarMode="sessions" />);
    expect(document.getElementById('sidebar-workspace-panel')).not.toBeVisible();
    rerender(<SessionList onSessionSelect={vi.fn()} sidebarMode="workspace" />);

    expect(screen.getByRole('button', { name: '收起 研究' })).toBeInTheDocument();
    expect(await screen.findByRole('button', { name: '最新报告.md' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '报告.md' })).not.toBeInTheDocument();
    expect(screen.getByRole('alert')).toHaveTextContent('1 项状态已失效');
    expect(screen.getByRole('button', { name: '删除' })).toBeDisabled();
    await waitFor(() => {
      expect(
        workspaceClient.get.mock.calls.filter(([url]) => url === '/workspace/entries'),
      ).toHaveLength(4);
    });
  });

  it('会话模式在同一固定模式栏右侧提供紧凑的新建对话入口', async () => {
    const onNewChat = vi.fn();
    const { rerender } = render(
      <SessionList onSessionSelect={vi.fn()} onNewChat={onNewChat} sidebarMode="sessions" />,
    );
    await screen.findByRole('tab', { name: '会话' });

    const modeActions = screen.getByTestId('session-mode-actions');
    expect(modeActions).toHaveClass('absolute', 'inset-y-0', 'right-1');
    const modeNewChat = within(modeActions).getByRole('button', { name: '新建对话' });
    expect(modeNewChat).toHaveClass('h-8', 'w-8');
    expect(modeNewChat).toHaveAttribute('title', '新建对话');
    fireEvent.click(modeNewChat);
    expect(onNewChat).toHaveBeenCalledTimes(1);

    const modeBarClass = screen.getByTestId('sidebar-mode-tabs').className;
    rerender(<SessionList onSessionSelect={vi.fn()} onNewChat={onNewChat} sidebarMode="workspace" />);
    expect(screen.queryByTestId('session-mode-actions')).not.toBeInTheDocument();
    expect(screen.getByTestId('sidebar-mode-tabs')).toHaveClass(...modeBarClass.split(' '));
    expect(await screen.findByTestId('workspace-panel-header')).toHaveClass('h-11', '-top-11');
  });

  it('移动端 Sheet 复用同一 tabs 与内容，并提供明确关闭按钮', async () => {
    const onClose = vi.fn();
    render(<SessionList mobileSheet sidebarMode="workspace" onSessionSelect={vi.fn()} onCloseMobileSheet={onClose} />);
    expect(await screen.findByTestId('workspace-sidebar-content')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '关闭侧栏' }));
    expect(onClose).toHaveBeenCalledTimes(1);
  });
});
