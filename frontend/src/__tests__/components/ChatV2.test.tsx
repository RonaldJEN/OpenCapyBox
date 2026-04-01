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
    uploadFile: vi.fn(),
    getRunningSession: vi.fn(),
    createSession: vi.fn(),
    getUserId: vi.fn(() => 'demo-session'),
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
  ArtifactsPanel: ({ isOpen, onClose, onFilePreview }: any) => (
    <div data-testid="artifacts-panel" data-open={String(isOpen)}>
      <button onClick={onClose}>Close Panel</button>
      <button onClick={() => onFilePreview({ name: 'test.pdf', path: '/test.pdf' })}>
        Preview File
      </button>
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

  it('点击面板中的文件应该打开预览', async () => {
    render(
      <ChatV2
        sessionId="test-session"
        {...defaultProps}
      />
    );

    // 打开 Artifacts 面板
    const folderButton = screen.getByTitle('会话资源');
    fireEvent.click(folderButton);

    // 点击预览文件按钮
    await waitFor(() => {
      fireEvent.click(screen.getByText('Preview File'));
    });

    // 应该显示文件预览
    await waitFor(() => {
      expect(screen.getByTestId('file-preview')).toBeInTheDocument();
      expect(screen.getByText('Preview: test.pdf')).toBeInTheDocument();
    });
  });

  it('应该能关闭文件预览', async () => {
    render(
      <ChatV2
        sessionId="test-session"
        {...defaultProps}
      />
    );

    // 打开 Artifacts 面板
    const folderButton = screen.getByTitle('会话资源');
    fireEvent.click(folderButton);

    // 打开预览
    await waitFor(() => {
      fireEvent.click(screen.getByText('Preview File'));
    });

    // 关闭预览
    await waitFor(() => {
      fireEvent.click(screen.getByText('Close Preview'));
    });

    // 预览应该关闭
    await waitFor(() => {
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
});
