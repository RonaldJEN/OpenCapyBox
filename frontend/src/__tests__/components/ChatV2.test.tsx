import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor, act } from '../utils/test-utils';
import { ChatV2 } from '../../components/ChatV2';
import { apiService } from '../../services/api';
import { RoundData } from '../../types';
import { makeChatV2DefaultProps } from '../utils/chatv2-helpers';

// Mock apiService
vi.mock('../../services/api', () => ({
  apiService: {
    getSessionHistoryV2: vi.fn(),
    getSessionFiles: vi.fn(),
    sendMessageStreamV2: vi.fn(),
    resumeStream: vi.fn(),
    uploadFile: vi.fn(),
    getRunningSession: vi.fn(),
    createSession: vi.fn(),
    abortChat: vi.fn(),
    getUserId: vi.fn(() => 'demo-session'),
    subscribeToRound: vi.fn(() => ({ abort: vi.fn(), promise: Promise.resolve() })),
  },
}));

// Mock 子组件
vi.mock('../../components/Round', () => ({
  Round: ({ round, isStreaming }: any) => (
    <div data-testid="round">
      <span>Round: {round.round_id}</span>
      <span>Streaming: {String(isStreaming)}</span>
      <span>User: {round.user_message}</span>
    </div>
  ),
}));

vi.mock('../../components/ArtifactsPanel', () => ({
  ArtifactsPanel: ({ isOpen, onClose }: any) => (
    <div data-testid="artifacts-panel" data-open={String(isOpen)}>
      <button onClick={onClose}>Close Panel</button>
    </div>
  ),
}));

vi.mock('../../components/FilePreview', () => ({
  FilePreview: ({ file, onClose }: any) => (
    <div data-testid="file-preview">
      <span>Preview: {file.name}</span>
      <button onClick={onClose}>Close Preview</button>
    </div>
  ),
}));

vi.mock('../../components/QuestionCard', () => ({
  QuestionCard: ({ onSubmit, onDismiss, disabled }: any) => (
    <div data-testid="question-card">
      <button
        type="button"
        disabled={disabled}
        onClick={() => onSubmit({ 'Which database should we use?': 'PostgreSQL' })}
      >
        Submit Question
      </button>
      {onDismiss && (
        <button type="button" data-testid="question-card-dismiss" onClick={onDismiss}>
          Dismiss
        </button>
      )}
    </div>
  ),
}));

describe('ChatV2 组件', () => {
  const mockRounds: RoundData[] = [
    {
      round_id: 'round-1',
      user_message: '你好',
      final_response: '你好！有什么可以帮助你的吗？',
      steps: [],
      step_count: 0,
      status: 'completed',
      created_at: new Date().toISOString(),
      completed_at: new Date().toISOString(),
    },
  ];

  const defaultProps = makeChatV2DefaultProps();

  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(apiService.getSessionHistoryV2).mockResolvedValue({
      rounds: mockRounds,
      session_id: 'test-session',
      total: mockRounds.length,
    });
    vi.mocked(apiService.getSessionFiles).mockResolvedValue({
      files: [],
      total: 0,
    });
    vi.mocked(apiService.resumeStream).mockResolvedValue(undefined);
  });

  it('没有 sessionId 时应该显示欢迎页（含输入框）', () => {
    render(
      <ChatV2
        sessionId=""
        {...defaultProps}
      />
    );

    // 欢迎标题
    expect(screen.getByText('你好，有什么可以帮你的？')).toBeInTheDocument();
    // 输入框应该存在
    expect(screen.getByPlaceholderText('输入你的问题，按 Enter 开始对话...')).toBeInTheDocument();
    // 快捷建议按钮
    expect(screen.getByText('帮我写一个 Python 爬虫')).toBeInTheDocument();
  });

  it('没有 sessionId 时不显示会话资源面板入口', () => {
    render(
      <ChatV2
        sessionId=""
        {...defaultProps}
      />
    );

    expect(screen.queryByTitle('会话资源')).not.toBeInTheDocument();
    expect(screen.queryByTestId('artifacts-panel')).not.toBeInTheDocument();
  });

  it('欢迎页点击快捷建议应该填入输入框', () => {
    render(
      <ChatV2
        sessionId=""
        {...defaultProps}
      />
    );

    fireEvent.click(screen.getByText('帮我写一个 Python 爬虫'));
    const textarea = screen.getByPlaceholderText('输入你的问题，按 Enter 开始对话...') as HTMLTextAreaElement;
    expect(textarea.value).toBe('帮我写一个 Python 爬虫');
  });

  it('欢迎页 Enter 发送应该调用 onCreateSession', async () => {
    const onCreateSession = vi.fn().mockResolvedValue('new-session-id');
    render(
      <ChatV2
        sessionId=""
        {...defaultProps}
        onCreateSession={onCreateSession}
      />
    );

    const textarea = screen.getByPlaceholderText('输入你的问题，按 Enter 开始对话...');
    fireEvent.change(textarea, { target: { value: '测试消息' } });
    fireEvent.keyDown(textarea, { key: 'Enter', shiftKey: false });

    await waitFor(() => {
      expect(onCreateSession).toHaveBeenCalled();
    });
  });

  it('欢迎页创建会话失败应该显示错误', async () => {
    const onCreateSession = vi.fn().mockRejectedValue(new Error('网络错误'));
    render(
      <ChatV2
        sessionId=""
        {...defaultProps}
        onCreateSession={onCreateSession}
      />
    );

    const textarea = screen.getByPlaceholderText('输入你的问题，按 Enter 开始对话...');
    fireEvent.change(textarea, { target: { value: '测试消息' } });
    fireEvent.keyDown(textarea, { key: 'Enter', shiftKey: false });

    await waitFor(() => {
      expect(screen.getByText('创建会话失败，请重试')).toBeInTheDocument();
    });
  });

  it('应该加载并显示历史记录', async () => {
    render(
      <ChatV2
        sessionId="test-session"
        {...defaultProps}
      />
    );

    await waitFor(() => {
      expect(apiService.getSessionHistoryV2).toHaveBeenCalledWith('test-session');
    });

    await waitFor(() => {
      expect(screen.getByText('Round: round-1')).toBeInTheDocument();
      expect(screen.getByText('User: 你好')).toBeInTheDocument();
    });
  });

  it('点击文件夹按钮应该打开 Artifacts 面板', async () => {
    render(
      <ChatV2
        sessionId="test-session"
        {...defaultProps}
      />
    );

    await waitFor(() => {
      expect(screen.getByTestId('artifacts-panel')).toBeInTheDocument();
    });

    // 初始状态面板关闭
    expect(screen.getByTestId('artifacts-panel')).toHaveAttribute('data-open', 'false');

    // 点击文件夹按钮
    const folderButton = screen.getByTitle('会话资源');
    fireEvent.click(folderButton);

    // 面板应该打开
    await waitFor(() => {
      expect(screen.getByTestId('artifacts-panel')).toHaveAttribute('data-open', 'true');
    });
  });

  it('打开面板不应该弹出全屏文件预览', async () => {
    render(
      <ChatV2
        sessionId="test-session"
        {...defaultProps}
      />
    );

    // 打开 Artifacts 面板
    const folderButton = screen.getByTitle('会话资源');
    fireEvent.click(folderButton);

    await waitFor(() => {
      expect(screen.getByTestId('artifacts-panel')).toHaveAttribute('data-open', 'true');
      expect(screen.queryByTestId('file-preview')).not.toBeInTheDocument();
    });
  });

  it('空历史时应该显示欢迎信息', async () => {
    vi.mocked(apiService.getSessionHistoryV2).mockResolvedValue({
      rounds: [],
      session_id: 'test-session',
      total: 0,
    });

    render(
      <ChatV2
        sessionId="test-session"
        {...defaultProps}
      />
    );

    await waitFor(() => {
      expect(screen.getByText('你好，有什么可以帮你的？')).toBeInTheDocument();
    });
  });

  it('应该显示输入框', async () => {
    render(
      <ChatV2
        sessionId="test-session"
        {...defaultProps}
      />
    );

    await waitFor(() => {
      expect(screen.getByPlaceholderText('输入指令...')).toBeInTheDocument();
    });
  });

  it('输入文本时发送按钮应该激活', async () => {
    render(
      <ChatV2
        sessionId="test-session"
        {...defaultProps}
      />
    );

    const textarea = screen.getByPlaceholderText('输入指令...');

    // 输入文本
    fireEvent.change(textarea, { target: { value: '测试消息' } });

    // 发送按钮应该进入激活样式
    await waitFor(() => {
      const sendButton = document.querySelector('button.bg-claude-text.text-white');
      expect(sendButton).toBeInTheDocument();
    });
  });

  it('应该显示底部版权信息', async () => {
    render(
      <ChatV2
        sessionId="test-session"
        {...defaultProps}
      />
    );

    await waitFor(() => {
      expect(screen.getByText(/OpenCapyBox · 内容由 AI 生成/)).toBeInTheDocument();
    });
  });

  it('加载中应该显示加载动画', () => {
    vi.mocked(apiService.getSessionHistoryV2).mockImplementation(
      () => new Promise(() => {}) // 永不 resolve
    );

    render(
      <ChatV2
        sessionId="test-session"
        {...defaultProps}
      />
    );

    // 检查是否有加载动画
    expect(document.querySelector('.animate-spin')).toBeInTheDocument();
  });

  it('切换会话时应该重新加载历史', async () => {
    const { rerender } = render(
      <ChatV2
        sessionId="session-1"
        {...defaultProps}
      />
    );

    await waitFor(() => {
      expect(apiService.getSessionHistoryV2).toHaveBeenCalledWith('session-1');
    });

    // 切换到新会话
    rerender(
      <ChatV2
        sessionId="session-2"
        {...defaultProps}
      />
    );

    await waitFor(() => {
      expect(apiService.getSessionHistoryV2).toHaveBeenCalledWith('session-2');
    });
  });

  it('从已有会话点击新建后应回到欢迎空状态', async () => {
    const { rerender } = render(
      <ChatV2
        sessionId="session-1"
        {...defaultProps}
      />
    );

    await waitFor(() => {
      expect(screen.getByText('Round: round-1')).toBeInTheDocument();
    });

    rerender(
      <ChatV2
        sessionId=""
        {...defaultProps}
      />
    );

    await waitFor(() => {
      expect(screen.getByText('你好，有什么可以帮你的？')).toBeInTheDocument();
      expect(screen.queryByText('Round: round-1')).not.toBeInTheDocument();
    });
  });

  it('欢迎页创建会话后应该自动发送暂存消息', async () => {
    // 模拟 loadHistory 返回空历史（新会话）
    vi.mocked(apiService.getSessionHistoryV2).mockResolvedValue({
      rounds: [],
      session_id: 'new-session',
      total: 0,
    });

    // 模拟 sendMessageStreamV2（需要能被调用到）
    vi.mocked(apiService.sendMessageStreamV2).mockImplementation(async () => {});

    // onCreateSession 模拟：返回新 sessionId
    const onCreateSession = vi.fn().mockResolvedValue('new-session');

    // 初始渲染：无 sessionId（欢迎页）
    const { rerender } = render(
      <ChatV2
        sessionId=""
        {...defaultProps}
        onCreateSession={onCreateSession}
      />
    );

    // 在欢迎页输入消息并发送
    const textarea = screen.getByPlaceholderText('输入你的问题，按 Enter 开始对话...');

    // 使用 act 确保 state 更新在 keyDown 之前已刷新
    await act(async () => {
      fireEvent.change(textarea, { target: { value: '自动发送测试' } });
    });
    await act(async () => {
      fireEvent.keyDown(textarea, { key: 'Enter', shiftKey: false });
    });

    // 等待 onCreateSession 被调用
    await waitFor(() => {
      expect(onCreateSession).toHaveBeenCalled();
    });

    // 模拟父组件设置了新的 sessionId（触发 rerender）
    await act(async () => {
      rerender(
        <ChatV2
          sessionId="new-session"
          {...defaultProps}
          onCreateSession={onCreateSession}
        />
      );
    });

    // loadHistory 完成后应该自动调用 sendMessageStreamV2
    await waitFor(() => {
      expect(apiService.sendMessageStreamV2).toHaveBeenCalledWith(
        'new-session',
        [{ type: 'text', text: '自动发送测试' }],
        expect.any(Object)
      );
    });
  });

  it('resume 失败后应恢复问题卡片以便重试', async () => {
    const interruptedRounds: RoundData[] = [
      {
        round_id: 'round-interrupted-1',
        user_message: '请帮我选数据库',
        final_response: '',
        steps: [],
        step_count: 0,
        status: 'interrupted',
        created_at: new Date().toISOString(),
        interrupt: {
          id: 'interrupt-1',
          reason: 'input_required',
          payload: {
            questions: [
              {
                question: 'Which database should we use?',
                header: 'Database',
                options: [
                  { label: 'PostgreSQL', description: 'Full SQL support' },
                  { label: 'SQLite', description: 'Lightweight' },
                ],
              },
            ],
          },
        },
      },
    ];

    vi.mocked(apiService.getSessionHistoryV2).mockResolvedValue({
      rounds: interruptedRounds,
      session_id: 'test-session',
      total: interruptedRounds.length,
    });
    vi.mocked(apiService.resumeStream).mockRejectedValue(new Error('resume failed'));

    render(
      <ChatV2
        sessionId="test-session"
        {...defaultProps}
      />
    );

    await waitFor(() => {
      expect(screen.getByTestId('question-card')).toBeInTheDocument();
    });

    fireEvent.click(screen.getByText('Submit Question'));

    await waitFor(() => {
      expect(apiService.resumeStream).toHaveBeenCalled();
    });

    await waitFor(() => {
      expect(screen.getByTestId('question-card')).toBeInTheDocument();
      expect(screen.getByText(/resume failed/)).toBeInTheDocument();
    });
  });

  it('interrupted 历史状态下输入框不应被锁死', async () => {
    const interruptedRounds: RoundData[] = [
      {
        round_id: 'round-interrupted-2',
        user_message: '请继续',
        final_response: '',
        steps: [],
        step_count: 0,
        status: 'interrupted',
        created_at: new Date().toISOString(),
        interrupt: {
          id: 'interrupt-2',
          reason: 'input_required',
          payload: {
            questions: [
              {
                question: 'Pick one?',
                header: 'Choice',
                options: [
                  { label: 'A', description: 'Option A' },
                  { label: 'B', description: 'Option B' },
                ],
              },
            ],
          },
        },
      },
    ];

    vi.mocked(apiService.getSessionHistoryV2).mockResolvedValue({
      rounds: interruptedRounds,
      session_id: 'test-session',
      total: interruptedRounds.length,
    });

    render(
      <ChatV2
        sessionId="test-session"
        {...defaultProps}
      />
    );

    await waitFor(() => {
      expect(screen.getByTestId('question-card')).toBeInTheDocument();
    });

    const textarea = screen.getByPlaceholderText('输入指令...') as HTMLTextAreaElement;
    expect(textarea).not.toBeDisabled();

    fireEvent.change(textarea, { target: { value: '直接发新消息跳过中断' } });
    expect(textarea.value).toBe('直接发新消息跳过中断');
  });

  it('切换 session 时 sending 状态应重置，输入框不应被锁死', async () => {
    // session-1 有一个运行中的轮次 → loadHistory 会设置 sending=true
    const runningRounds: RoundData[] = [
      {
        round_id: 'round-running-1',
        user_message: '运行中任务',
        final_response: '',
        steps: [],
        step_count: 0,
        status: 'running',
        created_at: new Date().toISOString(),
      },
    ];

    const idleRounds: RoundData[] = [
      {
        round_id: 'round-idle-1',
        user_message: '已完成任务',
        final_response: '完成',
        steps: [],
        step_count: 0,
        status: 'completed',
        created_at: new Date().toISOString(),
        completed_at: new Date().toISOString(),
      },
    ];

    vi.mocked(apiService.getSessionHistoryV2).mockImplementation(async (sid: string) => {
      if (sid === 'session-running') {
        return { rounds: runningRounds, session_id: sid, total: runningRounds.length };
      }
      return { rounds: idleRounds, session_id: sid, total: idleRounds.length };
    });

    // 渲染 session-running（会自动调 loadHistory → sending=true）
    const { rerender } = render(
      <ChatV2
        sessionId="session-running"
        {...defaultProps}
      />
    );

    await waitFor(() => {
      expect(apiService.getSessionHistoryV2).toHaveBeenCalledWith('session-running');
    });

    // 切换到 session-idle
    rerender(
      <ChatV2
        sessionId="session-idle"
        {...defaultProps}
      />
    );

    await waitFor(() => {
      expect(apiService.getSessionHistoryV2).toHaveBeenCalledWith('session-idle');
    });

    // 切换后输入框不应被禁用
    await waitFor(() => {
      const textarea = screen.getByPlaceholderText('输入指令...') as HTMLTextAreaElement;
      expect(textarea).not.toBeDisabled();
    });

    // onExecutionEnd 应被调用以清除 session-idle 的执行标记
    expect(defaultProps.onExecutionEnd).toHaveBeenCalledWith('session-idle');
  });

  it('从运行中 session 切换后，onExecutionEnd 应带 sessionId 参数', async () => {
    const runningRounds: RoundData[] = [
      {
        round_id: 'round-r1',
        user_message: '测试',
        final_response: '',
        steps: [],
        step_count: 0,
        status: 'running',
        created_at: new Date().toISOString(),
      },
    ];

    vi.mocked(apiService.getSessionHistoryV2).mockImplementation(async (sid: string) => {
      if (sid === 'sess-a') {
        return { rounds: runningRounds, session_id: sid, total: 1 };
      }
      return { rounds: [], session_id: sid, total: 0 };
    });

    const { rerender } = render(
      <ChatV2
        sessionId="sess-a"
        {...defaultProps}
      />
    );

    await waitFor(() => {
      expect(defaultProps.onExecutionStart).toHaveBeenCalledWith('sess-a');
    });

    // 切换到空闲 session
    rerender(
      <ChatV2
        sessionId="sess-b"
        {...defaultProps}
      />
    );

    await waitFor(() => {
      // onExecutionEnd 带有 sessionId，App 层只在匹配时清除
      expect(defaultProps.onExecutionEnd).toHaveBeenCalledWith('sess-b');
    });
  });

  it('handleStop 应该立即更新 UI 状态而不等待 SSE', async () => {
    // 模拟一个运行中的轮次
    const runningRounds: RoundData[] = [
      {
        round_id: 'round-running-stop',
        user_message: '运行中',
        final_response: '',
        steps: [
          {
            step_number: 1,
            thinking: '',
            assistant_content: '正在处理...',
            tool_calls: [],
            tool_results: [],
            status: 'running',
          },
        ],
        step_count: 1,
        status: 'running',
        created_at: new Date().toISOString(),
      },
    ];

    // subscribeToRound 不会推送 RUN_FINISHED，模拟 SSE 永远不结束
    const mockAbort = vi.fn();
    vi.mocked(apiService.subscribeToRound).mockReturnValue({
      abort: mockAbort,
      promise: new Promise(() => {}), // 永不 resolve
    } as any);

    vi.mocked(apiService.getSessionHistoryV2).mockResolvedValue({
      rounds: runningRounds,
      session_id: 'test-session',
      total: runningRounds.length,
    });

    vi.mocked(apiService.abortChat).mockResolvedValue(undefined);

    render(
      <ChatV2
        sessionId="test-session"
        {...defaultProps}
      />
    );

    // 等待 loadHistory 完成并进入 sending 状态
    await waitFor(() => {
      expect(apiService.subscribeToRound).toHaveBeenCalled();
    });

    // 应该看到停止按钮
    await waitFor(() => {
      expect(screen.getByTitle('停止生成')).toBeInTheDocument();
    });

    // 点击停止按钮
    await act(async () => {
      fireEvent.click(screen.getByTitle('停止生成'));
    });

    // abort API 应该被调用
    expect(apiService.abortChat).toHaveBeenCalledWith('test-session');

    // UI 应该立即更新 — 不再显示停止按钮，输入框可用
    await waitFor(() => {
      expect(screen.queryByTitle('停止生成')).not.toBeInTheDocument();
    });

    const textarea = screen.getByPlaceholderText('输入指令...') as HTMLTextAreaElement;
    expect(textarea).not.toBeDisabled();

    // onExecutionEnd 应该被调用
    expect(defaultProps.onExecutionEnd).toHaveBeenCalled();
  });

  it('user_cancelled 终态也应触发 onExecutionEnd，避免侧栏执行态残留', async () => {
    vi.mocked(apiService.getSessionHistoryV2).mockResolvedValue({
      rounds: [],
      session_id: 'test-session',
      total: 0,
    });

    vi.mocked(apiService.sendMessageStreamV2).mockImplementation(async (_sid, _content, callbacks) => {
      callbacks.onRunStarted?.('test-session', 'cancel-run-1');
      callbacks.onRunFinished?.(
        'test-session',
        'cancel-run-1',
        { finalResponse: '已取消', reason: 'user_cancelled' },
        'interrupt',
        undefined
      );
    });

    render(
      <ChatV2
        sessionId="test-session"
        {...defaultProps}
      />
    );

    const textarea = screen.getByPlaceholderText('输入指令...') as HTMLTextAreaElement;

    await act(async () => {
      fireEvent.change(textarea, { target: { value: '触发取消态' } });
    });

    await act(async () => {
      fireEvent.keyDown(textarea, { key: 'Enter', shiftKey: false });
    });

    await waitFor(() => {
      expect(defaultProps.onExecutionEnd).toHaveBeenCalledWith('test-session');
    });
  });

  it('abort 时应清理残留的 pendingInterrupt（QuestionCard 不应残留）', async () => {
    // 模拟一个处于 interrupted 状态的轮次（有 ask_user 中断）
    const interruptedRounds: RoundData[] = [
      {
        round_id: 'round-int-1',
        user_message: '分析一下',
        final_response: '',
        steps: [],
        step_count: 1,
        status: 'interrupted',
        created_at: new Date().toISOString(),
        interrupt: {
          id: 'int-001',
          reason: 'input_required',
          payload: {
            questions: [{ question: '你想深入了解哪个方面？', options: [{ label: '选项A' }] }],
          },
        },
      },
    ];

    vi.mocked(apiService.getSessionHistoryV2).mockResolvedValue({
      rounds: interruptedRounds,
      session_id: 'test-session',
      total: 1,
    });

    // 模拟 subscribeToRound 立即完成（不需要真正订阅）
    vi.mocked(apiService.subscribeToRound).mockReturnValue({
      abort: vi.fn(),
      promise: Promise.resolve(),
    } as any);

    vi.mocked(apiService.abortChat).mockResolvedValue(undefined);

    render(
      <ChatV2
        sessionId="test-session"
        {...defaultProps}
      />
    );

    // 等待历史加载并渲染 QuestionCard
    await waitFor(() => {
      expect(screen.getByTestId('question-card')).toBeInTheDocument();
    });

    // QuestionCard 应该可见
    expect(screen.getByTestId('question-card')).toBeInTheDocument();

    // 模拟：用户开始了新的执行（发送新消息），此时 sending=true
    // 然后触发了一个带 outcome=interrupt 但无 interrupt 的 RUN_FINISHED（用户取消）
    // 这会触发 sendMessageForSession 中的 setPendingInterrupt(null)    // 直接验证：发送新消息会清除 pendingInterrupt
    const textarea = screen.getByPlaceholderText('输入指令...') as HTMLTextAreaElement;

    // sendMessageStreamV2 模拟立即完成
    vi.mocked(apiService.sendMessageStreamV2).mockImplementation(async (_sid, _content, callbacks) => {
      callbacks.onRunStarted?.('test-session', 'new-round-1');
      callbacks.onRunFinished?.('test-session', 'new-round-1', { finalResponse: '完成' }, 'success', undefined);
    });

    await act(async () => {
      fireEvent.change(textarea, { target: { value: '新消息' } });
    });

    await act(async () => {
      fireEvent.keyDown(textarea, { key: 'Enter', shiftKey: false });
    });

    // 发送新消息后 QuestionCard 应该消失
    await waitFor(() => {
      expect(screen.queryByTestId('question-card')).not.toBeInTheDocument();
    });
  });

  it('点击 QuestionCard dismiss 按钮应本地隐藏卡片，不调后端接口', async () => {
    const interruptedRounds: RoundData[] = [
      {
        round_id: 'round-dismiss-1',
        user_message: '分析一下',
        final_response: '',
        steps: [],
        step_count: 1,
        status: 'interrupted',
        created_at: new Date().toISOString(),
        interrupt: {
          id: 'int-dismiss-001',
          reason: 'input_required',
          payload: {
            questions: [{ question: '选择方案', options: [{ label: 'A' }] }],
          },
        },
      },
    ];

    vi.mocked(apiService.getSessionHistoryV2).mockResolvedValue({
      rounds: interruptedRounds,
      session_id: 'test-session',
      total: 1,
    });

    render(<ChatV2 sessionId="test-session" {...defaultProps} />);

    // 等待 QuestionCard 出现
    await waitFor(() => {
      expect(screen.getByTestId('question-card')).toBeInTheDocument();
    });

    // 点击 dismiss 按钮
    await act(async () => {
      fireEvent.click(screen.getByTestId('question-card-dismiss'));
    });

    // QuestionCard 应该消失
    expect(screen.queryByTestId('question-card')).not.toBeInTheDocument();

    // 不应调用 resume 或 cancel 接口
    expect(apiService.resumeStream).not.toHaveBeenCalled();
    expect(apiService.abortChat).not.toHaveBeenCalled();
  });

  it('abort 请求失败时应保持运行状态，不做本地终止', async () => {
    const runningRounds: RoundData[] = [
      {
        round_id: 'round-abort-fail',
        user_message: '运行中',
        final_response: '',
        steps: [
          {
            step_number: 1,
            thinking: '',
            assistant_content: '处理中...',
            tool_calls: [],
            tool_results: [],
            status: 'running',
          },
        ],
        step_count: 1,
        status: 'running',
        created_at: new Date().toISOString(),
      },
    ];

    const mockAbort = vi.fn();
    vi.mocked(apiService.subscribeToRound).mockReturnValue({
      abort: mockAbort,
      promise: new Promise(() => {}), // 永不 resolve
    } as any);

    vi.mocked(apiService.getSessionHistoryV2).mockResolvedValue({
      rounds: runningRounds,
      session_id: 'test-session',
      total: runningRounds.length,
    });

    // abort API 抛出网络错误
    vi.mocked(apiService.abortChat).mockRejectedValue(new Error('Network Error'));

    render(
      <ChatV2
        sessionId="test-session"
        {...defaultProps}
      />
    );

    // 等待进入 sending 状态
    await waitFor(() => {
      expect(apiService.subscribeToRound).toHaveBeenCalled();
    });

    await waitFor(() => {
      expect(screen.getByTitle('停止生成')).toBeInTheDocument();
    });

    // 点击停止按钮
    await act(async () => {
      fireEvent.click(screen.getByTitle('停止生成'));
    });

    // abort API 被调用
    expect(apiService.abortChat).toHaveBeenCalledWith('test-session');

    // 关键断言：abort 失败后，UI 应保持运行状态
    // 停止按钮仍然可见（仍在 sending 状态）
    await waitFor(() => {
      expect(screen.getByTitle('停止生成')).toBeInTheDocument();
    });

    // 输入框仍被禁用
    const textarea = screen.getByPlaceholderText('输入指令...') as HTMLTextAreaElement;
    expect(textarea).toBeDisabled();

    // onExecutionEnd 不应被调用
    expect(defaultProps.onExecutionEnd).not.toHaveBeenCalled();
  });

  it('abort 失败但终态事件已到达时，不应丢失收敛回调', async () => {
    const runningRounds: RoundData[] = [
      {
        round_id: 'round-abort-race',
        user_message: '运行中',
        final_response: '',
        steps: [
          {
            step_number: 1,
            thinking: '',
            assistant_content: '处理中...',
            tool_calls: [],
            tool_results: [],
            status: 'running',
          },
        ],
        step_count: 1,
        status: 'running',
        created_at: new Date().toISOString(),
      },
    ];

    let subscribeCallbacks: any = null;
    vi.mocked(apiService.subscribeToRound).mockImplementation((_sid, _rid, callbacks: any) => {
      subscribeCallbacks = callbacks;
      return {
        abort: vi.fn(),
        promise: new Promise(() => {}),
      } as any;
    });

    vi.mocked(apiService.getSessionHistoryV2).mockResolvedValue({
      rounds: runningRounds,
      session_id: 'test-session',
      total: runningRounds.length,
    });

    vi.mocked(apiService.abortChat).mockImplementation(async () => {
      // 模拟：点击停止后 abort 请求失败，但几乎同时收到了后端终态事件。
      subscribeCallbacks?.onRunFinished?.(
        'test-session',
        'round-abort-race',
        { finalResponse: '已完成' },
        'success',
        undefined
      );
      throw new Error('Network Error');
    });

    render(
      <ChatV2
        sessionId="test-session"
        {...defaultProps}
      />
    );

    await waitFor(() => {
      expect(apiService.subscribeToRound).toHaveBeenCalled();
      expect(screen.getByTitle('停止生成')).toBeInTheDocument();
    });

    await act(async () => {
      fireEvent.click(screen.getByTitle('停止生成'));
    });

    // 终态已到达后，UI 应完成收敛，不能卡在 running。
    await waitFor(() => {
      expect(screen.queryByTitle('停止生成')).not.toBeInTheDocument();
    });

    const textarea = screen.getByPlaceholderText('输入指令...') as HTMLTextAreaElement;
    expect(textarea).not.toBeDisabled();
    expect(defaultProps.onExecutionEnd).toHaveBeenCalledWith('test-session');
  });

  it('abort 返回 409 时应按已停止处理，立即恢复 UI', async () => {
    const runningRounds: RoundData[] = [
      {
        round_id: 'round-abort-409',
        user_message: '运行中',
        final_response: '',
        steps: [
          {
            step_number: 1,
            thinking: '',
            assistant_content: '处理中...',
            tool_calls: [],
            tool_results: [],
            status: 'running',
          },
        ],
        step_count: 1,
        status: 'running',
        created_at: new Date().toISOString(),
      },
    ];

    const mockAbort = vi.fn();
    vi.mocked(apiService.subscribeToRound).mockReturnValue({
      abort: mockAbort,
      promise: new Promise(() => {}), // 永不 resolve
    } as any);

    vi.mocked(apiService.getSessionHistoryV2).mockResolvedValue({
      rounds: runningRounds,
      session_id: 'test-session',
      total: runningRounds.length,
    });

    const conflictError = Object.assign(new Error('Conflict'), {
      response: { status: 409 },
    });
    vi.mocked(apiService.abortChat).mockRejectedValue(conflictError);

    render(
      <ChatV2
        sessionId="test-session"
        {...defaultProps}
      />
    );

    await waitFor(() => {
      expect(apiService.subscribeToRound).toHaveBeenCalled();
      expect(screen.getByTitle('停止生成')).toBeInTheDocument();
    });

    await act(async () => {
      fireEvent.click(screen.getByTitle('停止生成'));
    });

    expect(apiService.abortChat).toHaveBeenCalledWith('test-session');

    await waitFor(() => {
      expect(screen.queryByTitle('停止生成')).not.toBeInTheDocument();
    });

    const textarea = screen.getByPlaceholderText('输入指令...') as HTMLTextAreaElement;
    expect(textarea).not.toBeDisabled();
    expect(defaultProps.onExecutionEnd).toHaveBeenCalled();
  });

  it('USER_BUSY (429) 不应触发 onExecutionStart，不污染侧栏执行标记', async () => {
    vi.mocked(apiService.getSessionHistoryV2).mockResolvedValue({
      rounds: [],
      session_id: 'test-session',
      total: 0,
    });

    // sendMessageStreamV2：直接调用 onRunError(USER_BUSY)，不调用 onRunStarted
    vi.mocked(apiService.sendMessageStreamV2).mockImplementation(async (_sid, _content, callbacks) => {
      callbacks.onRunError?.('当前有正在运行的任务', 'USER_BUSY');
    });

    render(
      <ChatV2
        sessionId="test-session"
        {...defaultProps}
      />
    );

    const textarea = screen.getByPlaceholderText('输入指令...') as HTMLTextAreaElement;

    await act(async () => {
      fireEvent.change(textarea, { target: { value: '测试并发被拒' } });
    });

    await act(async () => {
      fireEvent.keyDown(textarea, { key: 'Enter', shiftKey: false });
    });

    // 关键断言：onExecutionStart 不应被调用（因为 onRunStarted 未触发）
    expect(defaultProps.onExecutionStart).not.toHaveBeenCalled();

    // 错误信息应该展示
    await waitFor(() => {
      expect(screen.getByText(/正在运行/)).toBeInTheDocument();
    });

    // 输入框应恢复可用
    await waitFor(() => {
      expect(textarea).not.toBeDisabled();
    });
  });

  it('ask_user 中断事件不应泄漏到用户已切换到的新会话', async () => {
    // 模拟 session-a 空历史（将通过 sendMessageStreamV2 触发 interrupt）
    vi.mocked(apiService.getSessionHistoryV2).mockImplementation(async (sid: string) => {
      return { rounds: [], session_id: sid, total: 0 };
    });

    // sendMessageStreamV2 会在一段延迟后推送 interrupt，模拟 ask_user 延迟到达
    let capturedCallbacks: any = null;
    vi.mocked(apiService.sendMessageStreamV2).mockImplementation(async (_sid, _content, callbacks) => {
      capturedCallbacks = callbacks;
      callbacks.onRunStarted?.('session-a', 'round-ask-1');
      // 不立即推送 interrupt，等用户切换 session 后再推
    });

    const { rerender } = render(
      <ChatV2 sessionId="session-a" {...defaultProps} />
    );

    await waitFor(() => {
      expect(apiService.getSessionHistoryV2).toHaveBeenCalledWith('session-a');
    });

    const textarea = screen.getByPlaceholderText('输入指令...') as HTMLTextAreaElement;

    // 在 session-a 发送消息触发 ask_user
    await act(async () => {
      fireEvent.change(textarea, { target: { value: '问我个问题' } });
    });
    await act(async () => {
      fireEvent.keyDown(textarea, { key: 'Enter', shiftKey: false });
    });

    // 用户立即切换到 session-b（在 interrupt 到达之前）
    rerender(<ChatV2 sessionId="session-b" {...defaultProps} />);

    await waitFor(() => {
      expect(apiService.getSessionHistoryV2).toHaveBeenCalledWith('session-b');
    });

    // 此时旧 session-a 的 SSE 推送 interrupt 到达
    await act(async () => {
      capturedCallbacks?.onRunFinished?.(
        'session-a',
        'round-ask-1',
        { finalResponse: '' },
        'interrupt',
        { id: 'int-leak', reason: 'input_required', payload: { questions: [{ question: '泄漏测试' }] } }
      );
    });

    // 关键断言：QuestionCard 不应出现（interrupt 属于 session-a，不应污染 session-b）
    expect(screen.queryByTestId('question-card')).not.toBeInTheDocument();
  });

  it('stale RUN_FINISHED 应触发 onExecutionEnd(旧 sessionId) 释放侧栏执行标记', async () => {
    // session-a 空历史，通过 sendMessageStreamV2 开始执行
    vi.mocked(apiService.getSessionHistoryV2).mockImplementation(async (sid: string) => {
      return { rounds: [], session_id: sid, total: 0 };
    });

    let capturedCallbacks: any = null;
    vi.mocked(apiService.sendMessageStreamV2).mockImplementation(async (_sid, _content, callbacks) => {
      capturedCallbacks = callbacks;
      callbacks.onRunStarted?.('session-a', 'round-stale-1');
      // 不立即完成，等用户切走后再推送 RUN_FINISHED
    });

    const { rerender } = render(
      <ChatV2 sessionId="session-a" {...defaultProps} />
    );

    await waitFor(() => {
      expect(apiService.getSessionHistoryV2).toHaveBeenCalledWith('session-a');
    });

    const textarea = screen.getByPlaceholderText('输入指令...') as HTMLTextAreaElement;

    // 在 session-a 发送消息
    await act(async () => {
      fireEvent.change(textarea, { target: { value: '开始执行' } });
    });
    await act(async () => {
      fireEvent.keyDown(textarea, { key: 'Enter', shiftKey: false });
    });

    // 确认 onExecutionStart 被调用
    expect(defaultProps.onExecutionStart).toHaveBeenCalledWith('session-a');

    // 重置 mock 计数
    vi.mocked(defaultProps.onExecutionEnd).mockClear();

    // 切换到 session-b
    rerender(<ChatV2 sessionId="session-b" {...defaultProps} />);

    await waitFor(() => {
      expect(apiService.getSessionHistoryV2).toHaveBeenCalledWith('session-b');
    });

    // 旧 session-a 的 RUN_FINISHED 迟到
    await act(async () => {
      capturedCallbacks?.onRunFinished?.(
        'session-a',
        'round-stale-1',
        { finalResponse: '完成了' },
        'success',
        undefined
      );
    });

    // 关键断言：stale 回调应触发 onExecutionEnd 释放 session-a 的执行标记
    expect(defaultProps.onExecutionEnd).toHaveBeenCalledWith('session-a');
  });
});
