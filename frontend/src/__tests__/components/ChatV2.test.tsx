import { describe, it, expect, vi, beforeEach } from 'vitest';
import { StrictMode, forwardRef, useImperativeHandle } from 'react';
import { render, screen, fireEvent, waitFor, act } from '../utils/test-utils';
import { ChatV2 } from '../../components/ChatV2';
import { apiService } from '../../services/api';
import { RoundData } from '../../types';
import { makeChatV2DefaultProps } from '../utils/chatv2-helpers';
import { startSendStream } from '../../services/chatStreamClient';
import {
  ChatRuntimeProvider,
  useChatRuntime,
} from '../../runtime/ChatRuntimeProvider';

const ABORT_WARNING = '远端副作用可能已经发生，请确认后再重试。';
const ABORT_RESPONSE = {
  status: 'cancelled' as const,
  request_id: 'abort-request',
  reason: 'force_aborted',
  outcome_warning: ABORT_WARNING,
};

const workspaceClient = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
  put: vi.fn(),
  patch: vi.fn(),
  delete: vi.fn(),
}));
const workspacePreviewControls = vi.hoisted(() => ({
  dirty: false,
  save: vi.fn(async (_options?: unknown) => ({ ok: true, stale: false })),
}));
const sessionFilesControls = vi.hoisted(() => ({
  dirty: false,
  save: vi.fn(async (_options?: unknown) => ({ ok: true, stale: false, failedPaths: [] as string[] })),
}));

// Mock apiService
vi.mock('../../services/api', () => ({
  apiService: {
    getSessionHistoryV2: vi.fn(),
    getSessionFiles: vi.fn(),
    sendMessageStreamV2: vi.fn(),
    resumeStream: vi.fn(),
    uploadFile: vi.fn(),
    getRunningSessions: vi.fn(),
    createSession: vi.fn(),
    abortChat: vi.fn(),
    getAxiosClient: vi.fn(() => workspaceClient),
    getUserId: vi.fn(() => 'demo-session'),
    subscribeToRound: vi.fn(() => ({ abort: vi.fn(), promise: Promise.resolve() })),
  },
}));

vi.mock('../../services/configApi', () => ({
  getSkills: vi.fn(async () => ({
    sandbox_status: 'available',
    skills: [
      {
        key: 'pdf',
        name: 'pdf',
        display_name: 'PDF 处理',
        description: '读取和生成 PDF',
        category: 'document',
        source: 'official',
        enabled: true,
      },
    ],
  })),
}));

vi.mock('../../services/chatStreamClient', async () => {
  const { apiService } = await import('../../services/api');

  const makeEnvelope = (args: any, event: any, meta?: any) => ({
    ownerSessionId: args.ownerSessionId,
    clientRunKey: args.clientRunKey,
    serverRunId: event?.runId,
    transportEpoch: args.transportEpoch,
    connectionId: args.connectionId,
    event,
    source: args.source,
    sequence: meta?.sequence ?? event?.sequence ?? event?._sequence,
    isAggregate: meta?.isAggregate ?? event?.isAggregate,
    eventId: event?.id,
    messageId: event?.messageId,
    toolCallId: event?.toolCallId,
    receivedAt: Date.now(),
  });

  const emit = (args: any, event: any, meta?: any) => {
    args.onEnvelope(makeEnvelope(args, event, meta));
  };

  const callbacksFor = (args: any) => ({
    onStreamAccepted: () => emit(args, { type: 'CUSTOM', name: 'stream_accepted', value: {} }),
    onRunStarted: (threadId: string, runId: string) => emit(args, { type: 'RUN_STARTED', threadId, runId }),
    onRunFinished: (
      threadId: string,
      runId: string,
      result: any,
      outcome: string,
      interrupt?: any,
      meta?: any,
    ) => (
      emit(args, { type: 'RUN_FINISHED', threadId, runId, result, outcome, interrupt }, meta)
    ),
    onRunError: (message: string, code?: string) => {
      args.onError?.(message, code);
      emit(args, { type: 'RUN_ERROR', message, code });
    },
    onStepStarted: (stepName: string, timestamp?: number) => emit(args, { type: 'STEP_STARTED', stepName, timestamp }),
    onStepFinished: (stepName: string, timestamp?: number) => emit(args, { type: 'STEP_FINISHED', stepName, timestamp }),
    onTextMessageStart: (messageId: string, role: string) => emit(args, { type: 'TEXT_MESSAGE_START', messageId, role }),
    onTextMessageContent: (messageId: string, delta: string, meta?: any) => (
      emit(args, { type: 'TEXT_MESSAGE_CONTENT', messageId, delta }, meta)
    ),
    onTextMessageEnd: (messageId: string) => emit(args, { type: 'TEXT_MESSAGE_END', messageId }),
    onThinkingStart: (messageId: string, timestamp?: number) => (
      emit(args, { type: 'THINKING_TEXT_MESSAGE_START', messageId, timestamp })
    ),
    onThinkingContent: (messageId: string, delta: string, meta?: any) => (
      emit(args, { type: 'THINKING_TEXT_MESSAGE_CONTENT', messageId, delta }, meta)
    ),
    onThinkingEnd: (messageId: string, timestamp?: number) => (
      emit(args, { type: 'THINKING_TEXT_MESSAGE_END', messageId, timestamp })
    ),
    onToolCallStart: (toolCallId: string, toolCallName: string, parentMessageId?: string, timestamp?: number) => (
      emit(args, { type: 'TOOL_CALL_START', toolCallId, toolCallName, parentMessageId, timestamp })
    ),
    onToolCallArgs: (toolCallId: string, delta: string, meta?: any) => emit(args, { type: 'TOOL_CALL_ARGS', toolCallId, delta }, meta),
    onToolCallEnd: (toolCallId: string, timestamp?: number) => emit(args, { type: 'TOOL_CALL_END', toolCallId, timestamp }),
    onToolCallResult: (messageId: string, toolCallId: string, content: string, timestamp?: number, executionTimeMs?: number) => (
      emit(args, { type: 'TOOL_CALL_RESULT', messageId, toolCallId, content, timestamp, executionTimeMs })
    ),
    onStateSnapshot: (snapshot: any) => emit(args, { type: 'STATE_SNAPSHOT', snapshot }),
    onStateDelta: (delta: any) => emit(args, { type: 'STATE_DELTA', delta }),
    onMessagesSnapshot: (messages: any) => emit(args, { type: 'MESSAGES_SNAPSHOT', messages }),
    onCustomEvent: (name: string, value: any) => emit(args, { type: 'CUSTOM', name, value }),
    onActivitySnapshot: (messageId: string, activityType: string, content: any) => (
      emit(args, { type: 'ACTIVITY_SNAPSHOT', messageId, activityType, content })
    ),
    onActivityDelta: (messageId: string, activityType: string, patch: any) => (
      emit(args, { type: 'ACTIVITY_DELTA', messageId, activityType, patch })
    ),
  });

  return {
    startSendStream: vi.fn((args: any) => ({
      abort: vi.fn(),
      promise: apiService.sendMessageStreamV2(args.ownerSessionId, args.content, callbacksFor(args)),
      getLatestSequence: () => 0,
    })),
    startResumeStream: vi.fn((args: any) => ({
      abort: vi.fn(),
      promise: apiService.resumeStream(args.ownerSessionId, args.interruptId, args.answers, callbacksFor(args)),
    })),
    startSubscribeStream: vi.fn((args: any) => {
      const subscription = apiService.subscribeToRound(
        args.ownerSessionId,
        args.serverRunId,
        callbacksFor(args),
        args.lastSequence || 0,
      );
      return {
        ...subscription,
        promise: subscription.promise.catch((error: any) => {
          args.onError?.(error?.message || '订阅连接已断开');
          throw error;
        }),
      };
    }),
  };
});

// Mock 子组件
vi.mock('../../components/Round', () => ({
  Round: ({ round, isStreaming }: any) => (
    <div
      data-testid="round"
      data-assistant={round.final_response}
      data-steps={JSON.stringify(round.steps)}
      data-status={round.status}
      data-preferred-skills={JSON.stringify(round.preferred_skills || [])}
      data-preferred-mcp={JSON.stringify(round.preferred_mcp_connections || [])}
    >
      <span>Round: {round.round_id}</span>
      <span>Streaming: {String(isStreaming)}</span>
      <span>User: {round.user_message}</span>
    </div>
  ),
}));

vi.mock('../../components/ArtifactsPanel', () => ({
  ArtifactsPanel: forwardRef(function MockArtifactsPanel({ sessionId, ownerEpoch = 0, isOpen, onClose, variant, isExpanded, onToggleExpanded }: any, ref) {
    useImperativeHandle(ref, () => ({
      ownerSessionId: sessionId,
      ownerEpoch,
      hasDirty: () => sessionFilesControls.dirty,
      pendingFileDrafts: () => (
        sessionFilesControls.dirty ? [{ source: 'session', path: 'dirty.md' }] : []
      ),
      saveDirty: async (owner: any, options: any) => ({
        ...owner,
        ...(await sessionFilesControls.save(options)),
      }),
    }), [ownerEpoch, sessionId]);
    if (!sessionId) return null;
    return (
      <div
        data-testid="artifacts-panel"
        data-open={String(isOpen)}
        data-variant={variant}
        data-expanded={String(isExpanded)}
      >
        <button onClick={onClose}>Close Panel</button>
        <button onClick={onToggleExpanded}>Toggle Expand</button>
      </div>
    );
  }),
}));

vi.mock('../../services/mcpApi', () => ({
  getMcpServers: vi.fn(async () => [{
    id: 'server-a',
    name: '东方财富数据',
    description: '查询金融市场实时数据',
    source: 'official',
    status: 'published',
    enabled: true,
    required: false,
    auth_type: 'none',
    credential_set: false,
    header_names: [],
    allow_private_network: false,
    allow_insecure_http: false,
    installation_id: 'installation-a',
    tools_count: 2,
    enabled_tools_count: 2,
    enabled_tools: null,
    disabled_tools: [],
    last_tested_at: null,
    last_error: null,
    created_at: null,
    updated_at: null,
    version: 1,
  }]),
}));

vi.mock('../../components/FilePreview', () => ({
  FilePreview: forwardRef(function MockFilePreview({ file, onClose }: any, ref) {
    useImperativeHandle(ref, () => ({
      ownerSessionId: 'workspace:persistent',
      ownerEpoch: 0,
      path: file.path,
      isDirty: () => workspacePreviewControls.dirty,
      saveDirty: async (owner: any, options: any) => ({ ...owner, path: file.path, ...(await workspacePreviewControls.save(options)) }),
    }), [file.path]);
    return (
      <div data-testid="file-preview">
        <span>Preview: {file.name}</span>
        <button onClick={onClose}>Close Preview</button>
      </div>
    );
  }),
}));

vi.mock('../../components/QuestionCard', async () => {
  const { useState } = await import('react');
  return {
    QuestionCard: ({ questions, onSubmit, onDismiss, disabled }: any) => {
      const [draft, setDraft] = useState('');
      const question = questions?.[0]?.question || 'Which database should we use?';
      return (
        <div data-testid="question-card">
          <span>{`Question: ${question}`}</span>
          <input
            aria-label="mock question answer"
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
          />
          <button
            type="button"
            disabled={disabled}
            onClick={() => onSubmit({ [question]: draft || 'PostgreSQL' })}
          >
            Submit Question
          </button>
          {onDismiss && (
            <button type="button" data-testid="question-card-dismiss" onClick={onDismiss}>
              Dismiss
            </button>
          )}
        </div>
      );
    },
  };
});

function RuntimeStreamOwnershipHarness() {
  const runtime = useChatRuntime();
  const projection = runtime.getSessionProjection('test-session');
  return (
    <>
      <span data-testid="runtime-loading">{String(projection.loading)}</span>
      <button
        type="button"
        onClick={() => {
          void runtime.sendMessage({
            sessionId: 'test-session',
            displayMessage: 'hello',
            content: [{ type: 'text', text: 'hello' }],
          });
        }}
      >
        start direct run
      </button>
      <button
        type="button"
        onClick={() => {
          void runtime.loadSessionHistory('test-session');
        }}
      >
        reload history
      </button>
    </>
  );
}

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
    workspacePreviewControls.dirty = false;
    workspacePreviewControls.save.mockReset().mockResolvedValue({ ok: true, stale: false });
    sessionFilesControls.dirty = false;
    sessionFilesControls.save.mockReset().mockResolvedValue({ ok: true, stale: false, failedPaths: [] });
    vi.mocked(apiService.getSessionHistoryV2).mockResolvedValue({
      rounds: mockRounds,
      session_id: 'test-session',
      total: mockRounds.length,
    });
    vi.mocked(apiService.getSessionFiles).mockResolvedValue({
      files: [],
      total: 0,
    });
    vi.mocked(apiService.getRunningSessions).mockResolvedValue({
      running_sessions: [],
    });
    vi.mocked(apiService.resumeStream).mockResolvedValue(undefined);
    vi.mocked(apiService.abortChat).mockResolvedValue(ABORT_RESPONSE);
    workspaceClient.get.mockResolvedValue({
      data: { items: [], next_cursor: null, workspace_revision: 1 },
    });
  });

  it('模型目录未给出 default_model 时不读取不存在的 reasoning draft', () => {
    expect(() => render(
      <ChatV2
        sessionId=""
        {...defaultProps}
        selectedModelId={undefined as unknown as string}
        availableModels={[]}
      />,
    )).not.toThrow();

    expect(screen.getByPlaceholderText('输入你的问题，按 Enter 开始对话...')).toBeInTheDocument();
  });

  it('工作区文件多选去重并以 workspace identity 发送，不上传或编码 Data URL', async () => {
    vi.mocked(apiService.getSessionHistoryV2).mockResolvedValue({
      rounds: [], session_id: 'test-session', total: 0,
    });
    vi.mocked(apiService.sendMessageStreamV2).mockResolvedValue(undefined);
    const workspaceEntry = {
      entry_id: 'entry-report', parent_id: null, name: '日报.md', kind: 'file', path: '研究/日报.md',
      size_bytes: 88, mime_type: 'text/markdown', sha256: 'hash', revision: 5,
      current_version_id: 'version-5', status: 'active',
      created_at: '2026-08-26T00:00:00Z', updated_at: '2026-08-26T00:00:00Z',
    };
    workspaceClient.get.mockResolvedValue({
      data: { items: [workspaceEntry], next_cursor: null, workspace_revision: 2 },
    });
    render(<ChatV2 sessionId="test-session" {...defaultProps} />);

    const selectWorkspaceFile = async () => {
      fireEvent.click(await screen.findByRole('button', { name: '添加内容' }));
      const menuItems = screen.getAllByRole('menuitem');
      expect(menuItems.map((item) => item.textContent?.trim())).toEqual([
        '工作区文件', '上传文件', '专家 Skills', '数据连接',
      ]);
      await waitFor(() => expect(screen.getByRole('menuitem', { name: '工作区文件' })).toHaveFocus());
      fireEvent.keyDown(screen.getByRole('menu', { name: '添加内容' }), { key: 'ArrowDown' });
      expect(screen.getByRole('menuitem', { name: '上传文件' })).toHaveFocus();
      fireEvent.click(screen.getByRole('menuitem', { name: '工作区文件' }));
      fireEvent.click(await screen.findByRole('button', { name: '日报.md' }));
      fireEvent.click(screen.getByRole('button', { name: '添加到对话' }));
    };
    await selectWorkspaceFile();
    await selectWorkspaceFile();
    expect(screen.getAllByRole('button', { name: '移除 日报.md' })).toHaveLength(1);

    const textarea = screen.getByPlaceholderText('输入指令...');
    fireEvent.change(textarea, { target: { value: '分析日报' } });
    fireEvent.keyDown(textarea, { key: 'Enter', shiftKey: false });

    await waitFor(() => expect(apiService.sendMessageStreamV2).toHaveBeenCalledTimes(1));
    expect(apiService.uploadFile).not.toHaveBeenCalled();
    expect(vi.mocked(apiService.sendMessageStreamV2).mock.calls[0][1]).toContainEqual({
      type: 'file',
      file: {
        source: 'workspace',
        entry_id: 'entry-report',
        name: '日报.md',
        mime_type: 'text/markdown',
        size: 88,
      },
    });
  });

  it('选择工作区文件夹时保留文件夹卡片并发送目录 snapshot 引用', async () => {
    vi.mocked(apiService.getSessionHistoryV2).mockResolvedValue({
      rounds: [], session_id: 'test-session', total: 0,
    });
    vi.mocked(apiService.sendMessageStreamV2).mockResolvedValue(undefined);
    const workspaceFolder = {
      entry_id: 'folder-research', parent_id: null, name: '研究', kind: 'directory', path: '研究',
      size_bytes: 0, mime_type: null, sha256: null, revision: 2, status: 'active',
      created_at: '2026-08-26T00:00:00Z', updated_at: '2026-08-26T00:00:00Z',
    };
    workspaceClient.get.mockResolvedValue({
      data: {
        items: [workspaceFolder],
        next_cursor: null,
        workspace_revision: 2,
      },
    });
    render(<ChatV2 sessionId="test-session" {...defaultProps} />);

    fireEvent.click(await screen.findByRole('button', { name: '添加内容' }));
    fireEvent.click(screen.getByRole('menuitem', { name: '工作区文件' }));
    fireEvent.click(await screen.findByRole('button', { name: '研究' }));
    fireEvent.click(screen.getByRole('button', { name: '添加到对话' }));
    expect(await screen.findByRole('button', { name: '移除 研究' })).toBeInTheDocument();
    expect(screen.getByText('文件夹')).toBeInTheDocument();

    const textarea = screen.getByPlaceholderText('输入指令...');
    fireEvent.change(textarea, { target: { value: '分析研究目录' } });
    fireEvent.keyDown(textarea, { key: 'Enter', shiftKey: false });

    await waitFor(() => expect(apiService.sendMessageStreamV2).toHaveBeenCalledTimes(1));
    expect(vi.mocked(apiService.sendMessageStreamV2).mock.calls[0][1]).toContainEqual({
      type: 'file',
      file: {
        source: 'workspace',
        entry_id: 'folder-research',
        kind: 'directory',
        name: '研究',
        mime_type: 'inode/directory',
        size: 0,
      },
    });
  });

  it('欢迎页选择工作区文件不提前建 Session，发送时随 draft 迁移', async () => {
    vi.mocked(apiService.sendMessageStreamV2).mockResolvedValue(undefined);
    workspaceClient.get.mockResolvedValue({
      data: {
        items: [{
          entry_id: 'entry-xlsx', parent_id: null, name: '模型.xlsx', kind: 'file', path: '模型.xlsx',
          size_bytes: 128, mime_type: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
          sha256: 'hash', revision: 3, status: 'active', created_at: 'now', updated_at: 'now',
        }],
        next_cursor: null,
        workspace_revision: 2,
      },
    });
    const onCreateSession = vi.fn().mockResolvedValue('created-session');
    const onSessionCreated = vi.fn();
    render(<ChatV2 sessionId="" {...defaultProps} onCreateSession={onCreateSession} onSessionCreated={onSessionCreated} />);

    fireEvent.click(screen.getByRole('button', { name: '添加内容' }));
    fireEvent.click(screen.getByRole('menuitem', { name: '工作区文件' }));
    fireEvent.click(await screen.findByRole('button', { name: '模型.xlsx' }));
    fireEvent.click(screen.getByRole('button', { name: '添加到对话' }));
    expect(onCreateSession).not.toHaveBeenCalled();

    fireEvent.keyDown(screen.getByPlaceholderText('输入你的问题，按 Enter 开始对话...'), { key: 'Enter', shiftKey: false });

    await waitFor(() => expect(onCreateSession).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(apiService.sendMessageStreamV2).toHaveBeenCalledTimes(1));
    expect(onSessionCreated).toHaveBeenCalledWith('created-session');
    expect(apiService.uploadFile).not.toHaveBeenCalled();
    expect(vi.mocked(apiService.sendMessageStreamV2).mock.calls[0][1]).toContainEqual(expect.objectContaining({
      type: 'file',
      file: expect.objectContaining({ source: 'workspace', entry_id: 'entry-xlsx' }),
    }));
  });

  it('从历史 Round 保留服务端 Skill 展示快照', async () => {
    vi.mocked(apiService.getSessionHistoryV2).mockResolvedValue({
      rounds: [{
        ...mockRounds[0],
        preferred_skills: [{ key: 'pdf', display_name: 'PDF 处理' }],
      }],
      session_id: 'test-session',
      total: 1,
    });

    render(<ChatV2 sessionId="test-session" {...defaultProps} />);

    const renderedRound = await screen.findByTestId('round');
    expect(JSON.parse(renderedRound.getAttribute('data-preferred-skills') || '[]')).toEqual([
      { key: 'pdf', display_name: 'PDF 处理' },
    ]);
  });

  it('发送后清空 composer Skill 草稿并在 optimistic Round 保留 key 快照', async () => {
    vi.mocked(apiService.getSessionHistoryV2).mockResolvedValue({
      rounds: [],
      session_id: 'test-session',
      total: 0,
    });
    vi.mocked(apiService.sendMessageStreamV2).mockResolvedValue(undefined);

    render(<ChatV2 sessionId="test-session" {...defaultProps} />);

    fireEvent.click(await screen.findByRole('button', { name: '添加内容' }));
    fireEvent.click(screen.getByRole('menuitem', { name: /专家 Skills/ }));
    fireEvent.click(await screen.findByText('PDF 处理'));
    expect(screen.getByLabelText('已选择本轮偏好')).toHaveTextContent('PDF 处理');

    const textarea = screen.getByPlaceholderText('输入指令...');
    fireEvent.change(textarea, { target: { value: '分析这份文档' } });
    fireEvent.keyDown(textarea, { key: 'Enter', shiftKey: false });

    await waitFor(() => expect(apiService.sendMessageStreamV2).toHaveBeenCalledTimes(1));
    expect(screen.queryByLabelText('已选择本轮偏好')).not.toBeInTheDocument();
    const renderedRound = await screen.findByTestId('round');
    expect(JSON.parse(renderedRound.getAttribute('data-preferred-skills') || '[]')).toEqual([
      { key: 'pdf', display_name: 'pdf' },
    ]);
  });

  it('发送后清空数据连接草稿并冻结 optimistic server id 快照', async () => {
    vi.mocked(apiService.getSessionHistoryV2).mockResolvedValue({
      rounds: [],
      session_id: 'test-session',
      total: 0,
    });
    vi.mocked(apiService.sendMessageStreamV2).mockResolvedValue(undefined);

    render(<ChatV2 sessionId="test-session" {...defaultProps} />);

    fireEvent.click(await screen.findByRole('button', { name: '添加内容' }));
    fireEvent.click(screen.getByRole('menuitem', { name: /数据连接/ }));
    fireEvent.click(await screen.findByText('东方财富数据'));
    expect(screen.getByLabelText('已选择本轮偏好')).toHaveTextContent('东方财富数据');

    const textarea = screen.getByPlaceholderText('输入指令...');
    fireEvent.change(textarea, { target: { value: '查询最新行情' } });
    fireEvent.keyDown(textarea, { key: 'Enter', shiftKey: false });

    await waitFor(() => expect(startSendStream).toHaveBeenCalled());
    const startSendCalls = vi.mocked(startSendStream).mock.calls;
    expect(startSendCalls[startSendCalls.length - 1]?.[0]).toEqual(
      expect.objectContaining({ preferredMcpServerIds: ['server-a'] }),
    );
    expect(screen.queryByLabelText('已选择本轮偏好')).not.toBeInTheDocument();
    const renderedRound = await screen.findByTestId('round');
    expect(JSON.parse(renderedRound.getAttribute('data-preferred-mcp') || '[]')).toEqual([
      { server_id: 'server-a', display_name: '东方财富数据' },
    ]);
  });

  it('按 session 隔离并恢复 MCP 偏好草稿', async () => {
    vi.mocked(apiService.getSessionHistoryV2).mockImplementation(async (targetSessionId: string) => ({
      rounds: [],
      session_id: targetSessionId,
      total: 0,
    }));
    const { rerender } = render(<ChatV2 sessionId="session-a" {...defaultProps} />);

    fireEvent.click(await screen.findByRole('button', { name: '添加内容' }));
    fireEvent.click(screen.getByRole('menuitem', { name: /数据连接/ }));
    fireEvent.click(await screen.findByText('东方财富数据'));
    expect(screen.getByLabelText('已选择本轮偏好')).toHaveTextContent('东方财富数据');

    rerender(<ChatV2 sessionId="session-b" {...defaultProps} />);
    await waitFor(() => {
      expect(screen.queryByLabelText('已选择本轮偏好')).not.toBeInTheDocument();
    });

    rerender(<ChatV2 sessionId="session-a" {...defaultProps} />);
    await waitFor(() => {
      expect(screen.getByLabelText('已选择本轮偏好')).toHaveTextContent('东方财富数据');
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

  it('没有 sessionId 时不显示查看文件入口', () => {
    render(
      <ChatV2
        sessionId=""
        {...defaultProps}
      />
    );

    expect(screen.queryByRole('button', { name: '查看文件' })).not.toBeInTheDocument();
    expect(screen.queryByTestId('artifacts-panel')).not.toBeInTheDocument();
  });

  it('带 scrollTarget 时加载历史后应定位到对应 round', async () => {
    render(
      <ChatV2
        sessionId="test-session"
        {...defaultProps}
        scrollTarget={{ sessionId: 'test-session', roundId: 'round-1', nonce: 1 }}
      />
    );

    await waitFor(() => {
      expect(screen.getByText('Round: round-1')).toBeInTheDocument();
    });

    await waitFor(() => {
      expect(Element.prototype.scrollIntoView).toHaveBeenCalledWith({
        behavior: 'smooth',
        block: 'center',
      });
    });
  });

  it('普通进入会话时应直接定位到底部', async () => {
    render(
      <ChatV2
        sessionId="test-session"
        {...defaultProps}
      />
    );

    await waitFor(() => {
      expect(screen.getByText('Round: round-1')).toBeInTheDocument();
    });

    await waitFor(() => {
      expect(Element.prototype.scrollIntoView).toHaveBeenCalledWith({ behavior: 'auto' });
    });
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
    const onSessionCreated = vi.fn();
    render(
      <ChatV2
        sessionId=""
        {...defaultProps}
        onCreateSession={onCreateSession}
        onSessionCreated={onSessionCreated}
      />
    );

    const textarea = screen.getByPlaceholderText('输入你的问题，按 Enter 开始对话...');
    fireEvent.change(textarea, { target: { value: '测试消息' } });
    fireEvent.keyDown(textarea, { key: 'Enter', shiftKey: false });

    await waitFor(() => {
      expect(onCreateSession).toHaveBeenCalled();
      expect(onSessionCreated).toHaveBeenCalledWith('new-session-id');
    });
  });

  it('欢迎页首条消息创建会话时不应显示开启对话动画', async () => {
    let resolveCreate!: (value: string) => void;
    const onCreateSession = vi.fn(() => new Promise<string>((resolve) => {
      resolveCreate = resolve;
    }));

    const { rerender } = render(
      <ChatV2
        sessionId=""
        {...defaultProps}
        onCreateSession={onCreateSession}
      />
    );

    const textarea = screen.getByPlaceholderText('输入你的问题，按 Enter 开始对话...');
    fireEvent.change(textarea, { target: { value: '测试过渡消息' } });
    fireEvent.keyDown(textarea, { key: 'Enter', shiftKey: false });

    expect(screen.queryByText('正在开启对话')).not.toBeInTheDocument();
    expect(screen.queryByText('开启中')).not.toBeInTheDocument();
    expect(screen.getByText('你好，有什么可以帮你的？')).toBeInTheDocument();

    await act(async () => {
      resolveCreate('new-session-id');
    });

    await act(async () => {
      rerender(
        <ChatV2
          sessionId="new-session-id"
          {...defaultProps}
          onCreateSession={onCreateSession}
        />
      );
    });

    expect(screen.queryByText('正在开启对话')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: '查看文件' })).not.toHaveClass('animate-fade-in');
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
      expect(screen.getByPlaceholderText('输入你的问题，按 Enter 开始对话...')).toHaveValue('测试消息');
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

  it('当前会话收到外部 active slot 时应后台重新校准历史', async () => {
    const { rerender } = render(
      <ChatV2
        sessionId="test-session"
        {...defaultProps}
        activeSlotSessionIds={new Set()}
      />
    );

    await waitFor(() => {
      expect(apiService.getSessionHistoryV2).toHaveBeenCalledTimes(1);
    });
    vi.mocked(apiService.getSessionHistoryV2).mockClear();

    rerender(
      <ChatV2
        sessionId="test-session"
        {...defaultProps}
        activeSlotSessionIds={new Set(['test-session'])}
      />
    );

    await waitFor(() => {
      expect(apiService.getSessionHistoryV2).toHaveBeenCalledTimes(1);
    });

    expect(screen.queryByText('正在同步会话...')).not.toBeInTheDocument();
    expect(screen.getByText('Round: round-1')).toBeInTheDocument();
  });

  it('停在底部时流式文本更新应持续滚到底部', async () => {
    vi.mocked(apiService.sendMessageStreamV2).mockImplementation(async (_sid, _content, callbacks) => {
      callbacks.onStreamAccepted?.();
      callbacks.onRunStarted?.('test-session', 'stream-round-1');
      callbacks.onTextMessageStart?.('msg-1', 'assistant');
      callbacks.onTextMessageContent?.('msg-1', '第一段');
      await Promise.resolve();
      callbacks.onTextMessageContent?.('msg-1', '第一段第二段');
      await Promise.resolve();
      callbacks.onTextMessageEnd?.('msg-1');
      callbacks.onRunFinished?.('test-session', 'stream-round-1', { finalResponse: '第一段第二段' }, 'success');
    });

    render(
      <ChatV2
        sessionId="test-session"
        {...defaultProps}
      />
    );

    const textarea = await screen.findByPlaceholderText('输入指令...');
    fireEvent.change(textarea, { target: { value: '测试流式粘底' } });
    fireEvent.keyDown(textarea, { key: 'Enter', shiftKey: false });

    await waitFor(() => {
      expect(Element.prototype.scrollIntoView).toHaveBeenCalledWith({ behavior: 'auto' });
    });
  });

  it('不在底部且正在生成时应提示新回复正在生成', async () => {
    vi.mocked(apiService.sendMessageStreamV2).mockImplementation(async (_sid, _content, callbacks) => {
      callbacks.onStreamAccepted?.();
      callbacks.onRunStarted?.('test-session', 'stream-round-live');
      return new Promise(() => {});
    });

    const { container } = render(
      <ChatV2
        sessionId="test-session"
        {...defaultProps}
      />
    );

    const textarea = await screen.findByPlaceholderText('输入指令...');
    fireEvent.change(textarea, { target: { value: '测试新回复提示' } });
    fireEvent.keyDown(textarea, { key: 'Enter', shiftKey: false });

    await waitFor(() => {
      expect(textarea).toBeDisabled();
    });

    const chatArea = container.querySelector('.overflow-y-auto.relative.bg-claude-bg') as HTMLDivElement;
    Object.defineProperty(chatArea, 'scrollTop', { configurable: true, value: 200 });
    Object.defineProperty(chatArea, 'scrollHeight', { configurable: true, value: 1200 });
    Object.defineProperty(chatArea, 'clientHeight', { configurable: true, value: 500 });
    fireEvent.scroll(chatArea);

    expect(await screen.findByText('新回复正在生成')).toBeInTheDocument();
    expect(screen.getByLabelText('新回复正在生成，回到底部')).toBeInTheDocument();
  });

  it('查看文件应该进入分栏且保留聊天输入框', async () => {
    const { container } = render(
      <ChatV2
        sessionId="test-session"
        {...defaultProps}
      />
    );

    await waitFor(() => {
      expect(screen.getByTestId('artifacts-panel')).toBeInTheDocument();
    });
    expect(screen.getByTestId('chat-message-column')).toHaveClass('mx-auto', 'w-full', 'max-w-5xl');
    expect(screen.getByTestId('chat-input-column')).toHaveClass('mx-auto', 'w-full', 'max-w-5xl');

    // 初始状态面板关闭
    expect(screen.getByTestId('artifacts-panel')).toHaveAttribute('data-open', 'false');

    const filesButton = screen.getByRole('button', { name: '查看文件' });
    expect(filesButton).toHaveClass('h-6', 'w-6');
    expect(screen.getByRole('button', { name: '收起面板' })).toBeInTheDocument();
    fireEvent.click(filesButton);

    await waitFor(() => {
      expect(screen.getByTestId('artifacts-panel')).toHaveAttribute('data-open', 'true');
      expect(screen.getByTestId('artifacts-panel')).toHaveAttribute('data-variant', 'workspace');
      expect(screen.getByPlaceholderText('输入指令...')).toBeInTheDocument();
      expect(container.querySelector('.session-files-shell')).toHaveAttribute('data-layout', 'split');
      expect(screen.getByRole('separator', { name: '调整聊天和文件面板宽度' })).toBeInTheDocument();
      expect(screen.getByRole('button', { name: '查看文件' })).toHaveAttribute('aria-expanded', 'true');
      expect(screen.getByRole('button', { name: '展开面板' })).toBeInTheDocument();
    });

    fireEvent.click(filesButton);
    expect(container.querySelector('.session-files-shell')).toHaveAttribute('data-layout', 'split');
  });

  it('工作区文件开合应该恢复打开前的聊天滚动位置', async () => {
    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValue({
      ok: true,
      text: async () => 'workspace note',
      headers: new Headers({ 'Content-Type': 'text/plain' }),
    } as Response);
    const workspaceTarget = {
      entry_id: 'workspace-note', parent_id: null, name: 'note.txt', kind: 'file' as const,
      path: 'note.txt', size_bytes: 14, mime_type: 'text/plain', sha256: 'hash', revision: 1,
      status: 'active' as const, created_at: 'now', updated_at: 'now',
    };

    try {
      const { container, rerender } = render(
        <ChatV2 sessionId="test-session" {...defaultProps} workspaceFileTarget={null} />
      );
      const chatArea = container.querySelector('div[class*="overflow-y-auto"][class*="bg-claude-bg"]') as HTMLDivElement;
      Object.defineProperty(chatArea, 'scrollTop', { configurable: true, writable: true, value: 240 });

      rerender(
        <ChatV2 sessionId="test-session" {...defaultProps} workspaceFileTarget={workspaceTarget} />
      );
      await waitFor(() => expect(screen.getByTestId('workspace-files-panel')).toBeInTheDocument());
      expect(chatArea.scrollTop).toBe(240);

      rerender(
        <ChatV2 sessionId="test-session" {...defaultProps} workspaceFileTarget={null} />
      );
      await waitFor(() => expect(screen.queryByTestId('workspace-files-panel')).not.toBeInTheDocument());
      expect(chatArea.scrollTop).toBe(240);
    } finally {
      fetchSpy.mockRestore();
    }
  });

  it('工作区整面板远端保存失败也立即关闭，草稿由后台队列继续同步', async () => {
    workspacePreviewControls.dirty = true;
    workspacePreviewControls.save.mockResolvedValue({ ok: false, stale: false });
    const workspaceTarget = {
      entry_id: 'workspace-note', parent_id: null, name: 'note.txt', kind: 'file' as const,
      path: 'note.txt', size_bytes: 14, mime_type: 'text/plain', sha256: 'hash', revision: 1,
      status: 'active' as const, created_at: 'now', updated_at: 'now',
    };
    const onWorkspaceFilesClose = vi.fn();
    const { rerender } = render(<ChatV2 sessionId="test-session" {...defaultProps} workspaceFileTarget={workspaceTarget} onWorkspaceFilesClose={onWorkspaceFilesClose} />);
    await waitFor(() => expect(screen.getByTestId('workspace-files-panel')).toBeInTheDocument());

    fireEvent.click(screen.getByRole('button', { name: '收起工作区文件' }));
    await waitFor(() => expect(workspacePreviewControls.save).toHaveBeenCalledTimes(1));
    expect(onWorkspaceFilesClose).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument();
    rerender(<ChatV2 sessionId="test-session" {...defaultProps} workspaceFileTarget={null} onWorkspaceFilesClose={onWorkspaceFilesClose} />);
    await waitFor(() => expect(screen.queryByTestId('workspace-files-panel')).not.toBeInTheDocument());
  });

  it('发送时同步抓取 Workspace dirty 草稿，但远端失败不阻止 Agent 启动', async () => {
    workspacePreviewControls.dirty = true;
    workspacePreviewControls.save.mockResolvedValue({ ok: false, stale: false });
    vi.mocked(apiService.getSessionHistoryV2).mockResolvedValue({
      rounds: [], session_id: 'test-session', total: 0,
    });
    vi.mocked(apiService.sendMessageStreamV2).mockResolvedValue(undefined);
    const workspaceTarget = {
      entry_id: 'workspace-send-guard', parent_id: null, name: 'guard.md', kind: 'file' as const,
      path: 'guard.md', size_bytes: 12, mime_type: 'text/markdown', sha256: 'hash', revision: 1,
      status: 'active' as const, created_at: 'now', updated_at: 'now',
    };
    render(<ChatV2 sessionId="test-session" {...defaultProps} workspaceFileTarget={workspaceTarget} />);
    await waitFor(() => expect(screen.getByTestId('workspace-files-panel')).toBeInTheDocument());
    const textarea = screen.getByPlaceholderText('输入指令...');
    fireEvent.change(textarea, { target: { value: '先保存再发送' } });
    fireEvent.keyDown(textarea, { key: 'Enter', shiftKey: false });

    await waitFor(() => expect(workspacePreviewControls.save).toHaveBeenCalledWith(undefined));
    await waitFor(() => expect(apiService.sendMessageStreamV2).toHaveBeenCalled());
    expect(textarea).toHaveValue('');
    expect(screen.queryByText(/非附件文件尚未完成同步/)).not.toBeInTheDocument();
    const latestSend = vi.mocked(startSendStream).mock.calls.slice(-1)[0]?.[0];
    expect(latestSend?.pendingFileDrafts).toEqual([
      { source: 'workspace', path: 'guard.md' },
    ]);
  });

  it('发送已附加的 dirty Workspace 文件时等待该文件保存成功', async () => {
    workspacePreviewControls.dirty = true;
    let resolveSave!: (value: { ok: boolean; stale: boolean }) => void;
    workspacePreviewControls.save.mockImplementation(() => new Promise((resolve) => {
      resolveSave = (value) => {
        workspacePreviewControls.dirty = false;
        resolve(value);
      };
    }));
    vi.mocked(apiService.getSessionHistoryV2).mockResolvedValue({
      rounds: [], session_id: 'test-session', total: 0,
    });
    vi.mocked(apiService.sendMessageStreamV2).mockResolvedValue(undefined);
    const workspaceTarget = {
      entry_id: 'workspace-attached', parent_id: null, name: 'attached.md', kind: 'file' as const,
      path: 'attached.md', size_bytes: 12, mime_type: 'text/markdown', sha256: 'hash', revision: 1,
      current_version_id: 'version-1', status: 'active' as const, created_at: 'now', updated_at: 'now',
    };
    workspaceClient.get.mockResolvedValue({
      data: { items: [workspaceTarget], next_cursor: null, workspace_revision: 1 },
    });
    render(<ChatV2 sessionId="test-session" {...defaultProps} workspaceFileTarget={workspaceTarget} />);
    await waitFor(() => expect(screen.getByTestId('workspace-files-panel')).toBeInTheDocument());
    fireEvent.click(screen.getByRole('button', { name: '添加内容' }));
    fireEvent.click(screen.getByRole('menuitem', { name: '工作区文件' }));
    fireEvent.click(await screen.findByRole('button', { name: 'attached.md' }));
    fireEvent.click(screen.getByRole('button', { name: '添加到对话' }));

    const textarea = screen.getByPlaceholderText('输入指令...');
    fireEvent.change(textarea, { target: { value: '读取附件' } });
    fireEvent.keyDown(textarea, { key: 'Enter', shiftKey: false });

    await waitFor(() => expect(workspacePreviewControls.save).toHaveBeenCalledTimes(1));
    expect(apiService.sendMessageStreamV2).not.toHaveBeenCalled();
    expect(screen.getByRole('button', { name: '发送消息' })).toBeDisabled();
    expect(screen.getByRole('button', { name: '发送消息' }).querySelector('.animate-spin')).not.toBeNull();

    await act(async () => resolveSave({ ok: true, stale: false }));

    await waitFor(() => expect(apiService.sendMessageStreamV2).toHaveBeenCalledTimes(1));
    expect(vi.mocked(apiService.sendMessageStreamV2).mock.calls[0][1]).toContainEqual({
      type: 'file',
      file: {
        source: 'workspace',
        entry_id: 'workspace-attached',
        name: 'attached.md',
        mime_type: 'text/markdown',
        size: 12,
      },
    });
  });

  it('窄宽也应该保留文件分栏、splitter 与聊天侧总开关', async () => {
    const { container } = render(<ChatV2 sessionId="test-session" {...defaultProps} />);
    const filesButton = screen.getByRole('button', { name: '查看文件' });

    expect(screen.getByRole('button', { name: '收起面板' })).toBeInTheDocument();
    fireEvent.click(filesButton);

    await waitFor(() => {
      expect(container.querySelector('.session-files-shell')).toHaveAttribute('data-layout', 'split');
      expect(screen.getByTestId('chat-pane')).toHaveAttribute('aria-hidden', 'false');
      expect(screen.getByTestId('chat-pane')).not.toHaveAttribute('inert');
      expect(screen.getByRole('separator', { name: '调整聊天和文件面板宽度' })).toBeInTheDocument();
      expect(screen.getByRole('button', { name: '展开面板' })).toBeInTheDocument();
    });

    fireEvent.click(screen.getByRole('button', { name: '展开面板' }));
    await waitFor(() => {
      expect(container.querySelector('.session-files-shell')).toHaveAttribute('data-layout', 'closed');
      expect(screen.getByRole('button', { name: '收起面板' })).toBeInTheDocument();
    });
  });

  it('文件面板应该在 split 与 full 之间切换', async () => {
    const { container } = render(
      <ChatV2
        sessionId="test-session"
        {...defaultProps}
      />
    );

    fireEvent.click(screen.getByRole('button', { name: '查看文件' }));
    await waitFor(() => {
      expect(container.querySelector('.session-files-shell')).toHaveAttribute('data-layout', 'split');
    });

    fireEvent.click(screen.getByRole('button', { name: 'Toggle Expand' }));
    await waitFor(() => {
      expect(container.querySelector('.session-files-shell')).toHaveAttribute('data-layout', 'full');
      expect(screen.getByTestId('chat-pane')).toHaveAttribute('aria-hidden', 'true');
      expect(screen.getByTestId('artifacts-panel')).toHaveAttribute('data-expanded', 'true');
    });

    fireEvent.click(screen.getByRole('button', { name: 'Toggle Expand' }));
    await waitFor(() => {
      expect(container.querySelector('.session-files-shell')).toHaveAttribute('data-layout', 'split');
      expect(screen.getByTestId('chat-pane')).toHaveAttribute('aria-hidden', 'false');
    });
  });

  it('文件布局应该按 session 隔离并在返回时恢复', async () => {
    const { container, rerender } = render(
      <ChatV2 sessionId="test-session" {...defaultProps} />
    );

    fireEvent.click(screen.getByRole('button', { name: '查看文件' }));
    await waitFor(() => {
      expect(container.querySelector('.session-files-shell')).toHaveAttribute('data-layout', 'split');
    });

    rerender(<ChatV2 sessionId="session-b" {...defaultProps} />);
    await waitFor(() => {
      expect(container.querySelector('.session-files-shell')).toHaveAttribute('data-layout', 'closed');
      expect(screen.getByTestId('artifacts-panel')).toHaveAttribute('data-open', 'false');
    });

    rerender(<ChatV2 sessionId="test-session" {...defaultProps} />);
    await waitFor(() => {
      expect(container.querySelector('.session-files-shell')).toHaveAttribute('data-layout', 'split');
      expect(screen.getByTestId('artifacts-panel')).toHaveAttribute('data-open', 'true');
    });
  });

  it('聊天面板按钮在 closed/split 间切换右侧文件工作台', async () => {
    const { container } = render(
      <ChatV2 sessionId="test-session" {...defaultProps} />
    );

    expect(screen.getByRole('button', { name: '收起面板' })).toHaveAttribute('aria-expanded', 'false');
    expect(screen.queryByRole('button', { name: '展开面板' })).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: '收起面板' }));
    await waitFor(() => {
      expect(container.querySelector('.session-files-shell')).toHaveAttribute('data-layout', 'split');
    });
    expect(screen.getByRole('button', { name: '展开面板' })).toHaveAttribute('aria-expanded', 'true');
    expect(screen.getByRole('separator', { name: '调整聊天和文件面板宽度' })).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: '展开面板' }));
    expect(container.querySelector('.session-files-shell')).toHaveAttribute('data-layout', 'closed');
    expect(screen.getByRole('separator', { name: '调整聊天和文件面板宽度' }))
      .toHaveAttribute('aria-valuenow', '100');
    expect(screen.getByRole('button', { name: '收起面板' })).toBeInTheDocument();
  });

  it('session 切换保留当前 split，关闭后再打开固定恢复 45/55', async () => {
    const { container, rerender } = render(
      <ChatV2 sessionId="session-a" {...defaultProps} />
    );

    fireEvent.click(screen.getByRole('button', { name: '查看文件' }));
    const splitter = await screen.findByRole('separator', { name: '调整聊天和文件面板宽度' });
    fireEvent.keyDown(splitter, { key: 'ArrowRight' });
    expect(container.querySelector('.session-files-shell')).toHaveStyle({
      '--session-files-chat-ratio': '47%',
    });
    fireEvent.click(screen.getByRole('button', { name: '展开面板' }));
    expect(container.querySelector('.session-files-shell')).toHaveAttribute('data-layout', 'closed');
    fireEvent.click(screen.getByRole('button', { name: '收起面板' }));
    expect(container.querySelector('.session-files-shell')).toHaveAttribute('data-layout', 'split');
    expect(container.querySelector('.session-files-shell')).toHaveStyle({
      '--session-files-chat-ratio': '45%',
    });

    rerender(<ChatV2 sessionId="session-b" {...defaultProps} />);
    expect(container.querySelector('.session-files-shell')).toHaveAttribute('data-layout', 'closed');
    fireEvent.click(screen.getByRole('button', { name: '查看文件' }));
    expect(container.querySelector('.session-files-shell')).toHaveAttribute('data-layout', 'split');
    expect(container.querySelector('.session-files-shell')).toHaveStyle({
      '--session-files-chat-ratio': '45%',
    });

    rerender(<ChatV2 sessionId="session-a" {...defaultProps} />);
    expect(container.querySelector('.session-files-shell')).toHaveAttribute('data-layout', 'split');
    expect(container.querySelector('.session-files-shell')).toHaveStyle({
      '--session-files-chat-ratio': '45%',
    });
  });

  it('splitter 到达端点时切换为 full/closed，并用最后可见比例重新展开', async () => {
    window.localStorage.setItem('opencapybox.sessionFiles.chatRatio', '48');
    const { container } = render(<ChatV2 sessionId="test-session" {...defaultProps} />);
    const shell = container.querySelector('.session-files-shell') as HTMLDivElement;
    vi.spyOn(shell, 'getBoundingClientRect').mockReturnValue({
      x: 100,
      y: 0,
      left: 100,
      right: 1100,
      top: 0,
      bottom: 600,
      width: 1000,
      height: 600,
      toJSON: () => ({}),
    });

    fireEvent.click(screen.getByRole('button', { name: '查看文件' }));
    let splitter = await screen.findByRole('separator', { name: '调整聊天和文件面板宽度' });
    fireEvent.keyDown(splitter, { key: 'End' });
    expect(container.querySelector('.session-files-shell')).toHaveAttribute('data-layout', 'closed');
    expect(screen.getByRole('button', { name: '查看文件' })).toHaveAttribute('aria-pressed', 'false');
    expect(screen.getByRole('button', { name: '收起面板' })).toBeInTheDocument();
    expect(screen.getByRole('separator', { name: '调整聊天和文件面板宽度' }))
      .toHaveAttribute('aria-valuenow', '100');

    fireEvent.click(screen.getByRole('button', { name: '收起面板' }));
    expect(container.querySelector('.session-files-shell')).toHaveAttribute('data-layout', 'split');
    expect(container.querySelector('.session-files-shell')).toHaveStyle({
      '--session-files-chat-ratio': '45%',
    });

    splitter = screen.getByRole('separator', { name: '调整聊天和文件面板宽度' });
    fireEvent.keyDown(splitter, { key: 'Home' });
    expect(container.querySelector('.session-files-shell')).toHaveAttribute('data-layout', 'full');
    splitter = screen.getByRole('separator', { name: '调整聊天和文件面板宽度' });
    expect(splitter).toHaveAttribute('aria-valuenow', '0');
    fireEvent.keyDown(splitter, { key: 'ArrowRight' });
    expect(container.querySelector('.session-files-shell')).toHaveAttribute('data-layout', 'split');
    expect(container.querySelector('.session-files-shell')).toHaveStyle({
      '--session-files-chat-ratio': '2%',
    });

    fireEvent.keyDown(splitter, { key: 'Home' });
    expect(container.querySelector('.session-files-shell')).toHaveAttribute('data-layout', 'full');

    vi.stubGlobal('PointerEvent', MouseEvent);
    fireEvent.pointerDown(splitter, { button: 0, pointerId: 1 });
    fireEvent.pointerMove(window, { clientX: 250, pointerId: 1 });
    expect(container.querySelector('.session-files-shell')).toHaveAttribute('data-layout', 'split');
    expect(container.querySelector('.session-files-shell')).toHaveStyle({
      '--session-files-chat-ratio': '15%',
    });
    fireEvent.pointerMove(window, { clientX: 500, pointerId: 1 });
    fireEvent.pointerUp(window, { pointerId: 1 });
    expect(container.querySelector('.session-files-shell')).toHaveAttribute('data-layout', 'split');
    expect(container.querySelector('.session-files-shell')).toHaveStyle({
      '--session-files-chat-ratio': '40%',
    });

    splitter = screen.getByRole('separator', { name: '调整聊天和文件面板宽度' });
    fireEvent.pointerDown(splitter, { button: 0, pointerId: 2 });
    fireEvent.pointerMove(window, { clientX: 1050, pointerId: 2 });
    fireEvent.pointerMove(window, { clientX: 1100, pointerId: 2 });
    fireEvent.pointerUp(window, { pointerId: 2 });
    expect(container.querySelector('.session-files-shell')).toHaveAttribute('data-layout', 'closed');
    vi.unstubAllGlobals();
    window.localStorage.removeItem('opencapybox.sessionFiles.chatRatio');
  });

  it('旧的 0/100 持久化比例不会让文件面板以零宽度重新打开', () => {
    window.localStorage.setItem('opencapybox.sessionFiles.chatRatio', '100');
    const { container } = render(<ChatV2 sessionId="test-session" {...defaultProps} />);

    fireEvent.click(screen.getByRole('button', { name: '查看文件' }));
    expect(container.querySelector('.session-files-shell')).toHaveAttribute('data-layout', 'split');
    expect(container.querySelector('.session-files-shell')).toHaveStyle({
      '--session-files-chat-ratio': '45%',
    });
    expect(screen.getByRole('button', { name: '查看文件' })).toHaveAttribute('aria-pressed', 'true');
    window.localStorage.removeItem('opencapybox.sessionFiles.chatRatio');
  });

  it('打开 Files 后切到新对话应该恢复欢迎页输入框', async () => {
    const { rerender } = render(
      <ChatV2
        sessionId="test-session"
        {...defaultProps}
      />
    );

    fireEvent.click(screen.getByRole('button', { name: '查看文件' }));

    await waitFor(() => {
      expect(screen.getByTestId('artifacts-panel')).toHaveAttribute('data-open', 'true');
      expect(screen.getByPlaceholderText('输入指令...')).toBeInTheDocument();
    });

    rerender(
      <ChatV2
        sessionId=""
        {...defaultProps}
      />
    );

    await waitFor(() => {
      expect(screen.queryByTestId('artifacts-panel')).not.toBeInTheDocument();
      expect(screen.getByPlaceholderText('输入你的问题，按 Enter 开始对话...')).toBeInTheDocument();
    });

    rerender(
      <ChatV2
        sessionId="test-session"
        {...defaultProps}
      />
    );
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
    const folderButton = screen.getByRole('button', { name: '查看文件' });
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

  it('输入文本超过上限时应提示明确且不发送', async () => {
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

    const longText = 'a'.repeat(10001);
    const textarea = screen.getByPlaceholderText('输入指令...') as HTMLTextAreaElement;

    await act(async () => {
      fireEvent.change(textarea, { target: { value: longText } });
    });

    await act(async () => {
      fireEvent.keyDown(textarea, { key: 'Enter', shiftKey: false });
    });

    expect(apiService.sendMessageStreamV2).not.toHaveBeenCalled();
    expect(textarea.value).toBe(longText);
    expect(screen.getByText(/消息太长（10001 字）/)).toBeInTheDocument();
    expect(screen.getByText(/当前最多支持 10000 字/)).toBeInTheDocument();
  });

  it('上传目标状态无法确认时应显示避免覆盖的明确提示', async () => {
    vi.mocked(apiService.getSessionHistoryV2).mockResolvedValue({
      rounds: [],
      session_id: 'test-session',
      total: 0,
    });
    vi.mocked(apiService.uploadFile).mockRejectedValue({
      response: {
        data: {
          detail: '文件保存失败: 无法确认上传目标是否存在: /home/user/sessions/test-session/report.txt',
        },
      },
    });

    const { container } = render(
      <ChatV2
        sessionId="test-session"
        {...defaultProps}
      />
    );

    const fileInput = container.querySelector('input[type="file"]') as HTMLInputElement;
    const file = new File(['hello'], 'report.txt', { type: 'text/plain' });

    await act(async () => {
      fireEvent.change(fileInput, { target: { files: [file] } });
    });

    expect(await screen.findByText('文件上传失败：无法确认目标文件是否已存在。为避免覆盖已有文件，本次上传已取消，请稍后重试。')).toBeInTheDocument();
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

  it('正文草稿应按 session 隔离并在切回时恢复', async () => {
    vi.mocked(apiService.getSessionHistoryV2).mockImplementation(async (targetSessionId: string) => ({
      rounds: [],
      session_id: targetSessionId,
      total: 0,
    }));

    const { rerender } = render(<ChatV2 sessionId="session-a" {...defaultProps} />);
    const sessionATextarea = await screen.findByPlaceholderText('输入指令...');
    fireEvent.change(sessionATextarea, { target: { value: 'A 会话草稿' } });

    rerender(<ChatV2 sessionId="session-b" {...defaultProps} />);
    await waitFor(() => {
      expect(apiService.getSessionHistoryV2).toHaveBeenCalledWith('session-b');
      expect(screen.getByPlaceholderText('输入指令...')).toHaveValue('');
    });
    fireEvent.change(screen.getByPlaceholderText('输入指令...'), { target: { value: 'B 会话草稿' } });

    rerender(<ChatV2 sessionId="session-a" {...defaultProps} />);
    await waitFor(() => {
      expect(screen.getByPlaceholderText('输入指令...')).toHaveValue('A 会话草稿');
    });
  });

  it('迟到的上传结果只能写回发起上传的 session 草稿', async () => {
    vi.mocked(apiService.getSessionHistoryV2).mockImplementation(async (targetSessionId: string) => ({
      rounds: [],
      session_id: targetSessionId,
      total: 0,
    }));
    let resolveUpload!: (file: {
      name: string;
      path: string;
      size: number;
      modified: string;
      type: string;
    }) => void;
    vi.mocked(apiService.uploadFile).mockImplementation(() => new Promise((resolve) => {
      resolveUpload = resolve;
    }));

    const { container, rerender } = render(<ChatV2 sessionId="session-a" {...defaultProps} />);
    const fileInput = container.querySelector('input[type="file"]') as HTMLInputElement;
    fireEvent.change(fileInput, {
      target: { files: [new File(['A'], 'session-a.txt', { type: 'text/plain' })] },
    });

    rerender(<ChatV2 sessionId="session-b" {...defaultProps} />);
    await act(async () => {
      resolveUpload({
        name: 'session-a.txt',
        path: 'session-a.txt',
        size: 1,
        modified: new Date().toISOString(),
        type: 'text/plain',
      });
    });

    expect(screen.queryByText('session-a.txt')).not.toBeInTheDocument();
    rerender(<ChatV2 sessionId="session-a" {...defaultProps} />);
    expect(await screen.findByText('session-a.txt')).toBeInTheDocument();
  });

  it('附件上传期间应锁定发送，避免上传回调写入发送后的草稿', async () => {
    vi.mocked(apiService.getSessionHistoryV2).mockResolvedValue({
      rounds: [],
      session_id: 'session-a',
      total: 0,
    });
    vi.mocked(apiService.sendMessageStreamV2).mockResolvedValue(undefined);
    let resolveUpload!: (file: {
      name: string;
      path: string;
      size: number;
      modified: string;
      type: string;
    }) => void;
    vi.mocked(apiService.uploadFile).mockImplementation(() => new Promise((resolve) => {
      resolveUpload = resolve;
    }));

    const { container } = render(<ChatV2 sessionId="session-a" {...defaultProps} />);
    const fileInput = container.querySelector('input[type="file"]') as HTMLInputElement;
    fireEvent.change(fileInput, {
      target: { files: [new File(['pending'], 'pending.txt', { type: 'text/plain' })] },
    });
    const textarea = screen.getByPlaceholderText('输入指令...');
    fireEvent.change(textarea, { target: { value: '不能抢跑发送' } });

    expect(screen.getByRole('button', { name: '发送消息' })).toBeDisabled();
    fireEvent.keyDown(textarea, { key: 'Enter', shiftKey: false });
    expect(apiService.sendMessageStreamV2).not.toHaveBeenCalled();

    await act(async () => {
      resolveUpload({
        name: 'pending.txt',
        path: 'pending.txt',
        size: 7,
        modified: new Date().toISOString(),
        type: 'text/plain',
      });
    });

    await waitFor(() => {
      expect(screen.getByText('pending.txt')).toBeInTheDocument();
      expect(screen.getByRole('button', { name: '发送消息' })).toBeEnabled();
    });
  });

  it('欢迎页并发上传只能触发一次隐式建会话', async () => {
    let resolveCreateSession!: (sessionId: string) => void;
    const onCreateSession = vi.fn(() => new Promise<string>((resolve) => {
      resolveCreateSession = resolve;
    }));
    const onSessionCreated = vi.fn();
    vi.mocked(apiService.uploadFile).mockResolvedValue({
      name: 'first.txt',
      path: 'first.txt',
      size: 5,
      modified: new Date().toISOString(),
      type: 'text/plain',
    });

    const { container } = render(
      <ChatV2
        sessionId=""
        {...defaultProps}
        onCreateSession={onCreateSession}
        onSessionCreated={onSessionCreated}
      />,
    );
    const fileInput = container.querySelector('input[type="file"]') as HTMLInputElement;
    fireEvent.change(fileInput, {
      target: { files: [new File(['first'], 'first.txt', { type: 'text/plain' })] },
    });
    fireEvent.change(fileInput, {
      target: { files: [new File(['second'], 'second.txt', { type: 'text/plain' })] },
    });

    expect(onCreateSession).toHaveBeenCalledTimes(1);
    await act(async () => {
      resolveCreateSession('created-session');
    });

    await waitFor(() => {
      expect(apiService.uploadFile).toHaveBeenCalledTimes(1);
      expect(apiService.uploadFile).toHaveBeenCalledWith('created-session', expect.objectContaining({
        name: 'first.txt',
      }));
      expect(onSessionCreated).toHaveBeenCalledTimes(1);
      expect(onSessionCreated).toHaveBeenCalledWith('created-session');
    });
  });

  it('迟到的隐式建会话响应不应抢回用户已切换的会话', async () => {
    vi.mocked(apiService.getSessionHistoryV2).mockImplementation(async (targetSessionId: string) => ({
      rounds: [],
      session_id: targetSessionId,
      total: 0,
    }));
    vi.mocked(apiService.sendMessageStreamV2).mockResolvedValue(undefined);
    let resolveCreateSession!: (sessionId: string) => void;
    const onCreateSession = vi.fn(() => new Promise<string>((resolve) => {
      resolveCreateSession = resolve;
    }));
    const onSessionCreated = vi.fn();

    const { rerender } = render(
      <ChatV2
        sessionId=""
        {...defaultProps}
        onCreateSession={onCreateSession}
        onSessionCreated={onSessionCreated}
      />,
    );
    const textarea = screen.getByPlaceholderText('输入你的问题，按 Enter 开始对话...');
    fireEvent.change(textarea, { target: { value: '后台继续发送' } });
    fireEvent.keyDown(textarea, { key: 'Enter', shiftKey: false });
    expect(onCreateSession).toHaveBeenCalledTimes(1);

    rerender(
      <ChatV2
        sessionId="session-b"
        {...defaultProps}
        onCreateSession={onCreateSession}
        onSessionCreated={onSessionCreated}
      />,
    );
    await act(async () => {
      resolveCreateSession('created-session');
    });

    await waitFor(() => {
      expect(apiService.sendMessageStreamV2).toHaveBeenCalledWith(
        'created-session',
        [{ type: 'text', text: '后台继续发送' }],
        expect.any(Object),
      );
      expect(screen.getByPlaceholderText('输入指令...')).toHaveValue('');
    });
    expect(onSessionCreated).not.toHaveBeenCalled();
  });

  it('欢迎页 createSession pending 时切换到其他会话，目标会话应可输入并发送', async () => {
    vi.mocked(apiService.getSessionHistoryV2).mockImplementation(async (targetSessionId: string) => ({
      rounds: [],
      session_id: targetSessionId,
      total: 0,
    }));
    vi.mocked(apiService.sendMessageStreamV2).mockResolvedValue(undefined);
    // createSession 一直 pending，模拟欢迎页首条消息建会话时后端慢响应
    const onCreateSession = vi.fn(() => new Promise<string>(() => {}));

    const { rerender } = render(
      <ChatV2 sessionId="" {...defaultProps} onCreateSession={onCreateSession} />,
    );
    const welcomeTextarea = screen.getByPlaceholderText('输入你的问题，按 Enter 开始对话...');
    fireEvent.change(welcomeTextarea, { target: { value: '欢迎页首条消息' } });
    fireEvent.keyDown(welcomeTextarea, { key: 'Enter', shiftKey: false });
    expect(onCreateSession).toHaveBeenCalledTimes(1);

    // 欢迎页建会话仍在进行时切换到已有会话 B
    rerender(
      <ChatV2 sessionId="session-b" {...defaultProps} onCreateSession={onCreateSession} />,
    );

    const textareaB = await screen.findByPlaceholderText('输入指令...');
    expect(textareaB).toBeEnabled();
    fireEvent.change(textareaB, { target: { value: 'B 会话消息' } });
    const sendButton = screen.getByRole('button', { name: '发送消息' });
    expect(sendButton).toBeEnabled();
    fireEvent.keyDown(textareaB, { key: 'Enter', shiftKey: false });

    await waitFor(() => {
      expect(apiService.sendMessageStreamV2).toHaveBeenCalledWith(
        'session-b',
        [{ type: 'text', text: 'B 会话消息' }],
        expect.any(Object),
      );
    });
  });

  it('session-a 上传 pending 时切换到 session-b，B 应能正常发送和上传', async () => {
    vi.mocked(apiService.getSessionHistoryV2).mockImplementation(async (targetSessionId: string) => ({
      rounds: [],
      session_id: targetSessionId,
      total: 0,
    }));
    vi.mocked(apiService.sendMessageStreamV2).mockResolvedValue(undefined);
    // A 会话的上传一直 pending，B 会话的上传正常完成
    vi.mocked(apiService.uploadFile).mockImplementation((targetSessionId: string) => {
      if (targetSessionId === 'session-a') return new Promise(() => {});
      return Promise.resolve({
        name: 'b.txt',
        path: 'b.txt',
        size: 1,
        modified: new Date().toISOString(),
        type: 'text/plain',
      });
    });

    const { container, rerender } = render(<ChatV2 sessionId="session-a" {...defaultProps} />);
    const fileInputA = container.querySelector('input[type="file"]') as HTMLInputElement;
    fireEvent.change(fileInputA, {
      target: { files: [new File(['A'], 'a.txt', { type: 'text/plain' })] },
    });
    await waitFor(() => {
      expect(apiService.uploadFile).toHaveBeenCalledWith('session-a', expect.any(File));
    });

    // A 上传仍 pending 时切换到 session-b
    rerender(<ChatV2 sessionId="session-b" {...defaultProps} />);
    const textareaB = await screen.findByPlaceholderText('输入指令...');

    // B 不受 A 的 pending 上传影响，可发起自己的上传
    const fileInputB = container.querySelector('input[type="file"]') as HTMLInputElement;
    fireEvent.change(fileInputB, {
      target: { files: [new File(['B'], 'b.txt', { type: 'text/plain' })] },
    });
    await waitFor(() => {
      expect(apiService.uploadFile).toHaveBeenCalledWith('session-b', expect.any(File));
      expect(screen.getByText('b.txt')).toBeInTheDocument();
    });

    // B 上传完成后仍可正常发送
    const sendButton = screen.getByRole('button', { name: '发送消息' });
    fireEvent.change(textareaB, { target: { value: 'B 消息' } });
    expect(sendButton).toBeEnabled();
    fireEvent.keyDown(textareaB, { key: 'Enter', shiftKey: false });
    await waitFor(() => {
      expect(apiService.sendMessageStreamV2).toHaveBeenCalledWith(
        'session-b',
        expect.any(Array),
        expect.any(Object),
      );
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
      expect(screen.getByPlaceholderText('输入你的问题，按 Enter 开始对话...')).toHaveFocus();
    });
  });

  it('隐式创建会话后应协调迁移正文、附件和统一偏好草稿', async () => {
    vi.mocked(apiService.getSessionHistoryV2).mockImplementation(async (targetSessionId: string) => ({
      rounds: [],
      session_id: targetSessionId,
      total: 0,
    }));
    vi.mocked(apiService.uploadFile).mockResolvedValue({
      name: 'combined.txt',
      path: 'combined.txt',
      size: 8,
      modified: new Date().toISOString(),
      type: 'text/plain',
    });
    const onCreateSession = vi.fn().mockResolvedValue('new-session');

    const { container, rerender } = render(
      <ChatV2 sessionId="" {...defaultProps} onCreateSession={onCreateSession} />,
    );
    fireEvent.click(screen.getByRole('button', { name: '添加内容' }));
    fireEvent.click(screen.getByRole('menuitem', { name: /专家 Skills/ }));
    fireEvent.click(await screen.findByText('PDF 处理'));
    fireEvent.click(screen.getByRole('button', { name: '添加内容' }));
    fireEvent.click(screen.getByRole('button', { name: '添加内容' }));
    fireEvent.click(screen.getByRole('menuitem', { name: /数据连接/ }));
    fireEvent.click(await screen.findByText('东方财富数据'));
    fireEvent.change(screen.getByPlaceholderText('输入你的问题，按 Enter 开始对话...'), {
      target: { value: '组合草稿' },
    });
    const fileInput = container.querySelector('input[type="file"]') as HTMLInputElement;
    fireEvent.change(fileInput, {
      target: { files: [new File(['combined'], 'combined.txt', { type: 'text/plain' })] },
    });

    await waitFor(() => {
      expect(apiService.uploadFile).toHaveBeenCalledWith('new-session', expect.any(File));
    });
    rerender(<ChatV2 sessionId="new-session" {...defaultProps} onCreateSession={onCreateSession} />);

    await waitFor(() => {
      expect(screen.getByPlaceholderText('输入指令...')).toHaveValue('组合草稿');
      expect(screen.getByText('combined.txt')).toBeInTheDocument();
      expect(screen.getByLabelText('已选择本轮偏好')).toHaveTextContent('PDF 处理');
      expect(screen.getByLabelText('已选择本轮偏好')).toHaveTextContent('东方财富数据');
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

  it('欢迎页创建会话后若在 stream accepted 前被拒绝，应恢复统一偏好草稿', async () => {
    vi.mocked(apiService.getSessionHistoryV2).mockImplementation(async (targetSessionId: string) => ({
      rounds: [],
      session_id: targetSessionId,
      total: 0,
    }));

    let rejectSend!: (reason?: unknown) => void;
    vi.mocked(apiService.sendMessageStreamV2).mockImplementation(() => new Promise((_resolve, reject) => {
      rejectSend = reject;
    }));
    const onCreateSession = vi.fn().mockResolvedValue('new-session');

    const { rerender } = render(
      <ChatV2
        sessionId=""
        {...defaultProps}
        onCreateSession={onCreateSession}
      />
    );

    fireEvent.click(screen.getByRole('button', { name: '添加内容' }));
    fireEvent.click(screen.getByRole('menuitem', { name: /专家 Skills/ }));
    fireEvent.click(await screen.findByText('PDF 处理'));
    expect(screen.getByLabelText('已选择本轮偏好')).toHaveTextContent('PDF 处理');
    fireEvent.click(screen.getByRole('button', { name: '添加内容' }));
    fireEvent.click(screen.getByRole('button', { name: '添加内容' }));
    fireEvent.click(screen.getByRole('menuitem', { name: /数据连接/ }));
    fireEvent.click(await screen.findByText('东方财富数据'));
    expect(screen.getByLabelText('已选择本轮偏好')).toHaveTextContent('东方财富数据');

    const textarea = screen.getByPlaceholderText('输入你的问题，按 Enter 开始对话...');
    fireEvent.change(textarea, { target: { value: '触发发送前拒绝' } });
    fireEvent.keyDown(textarea, { key: 'Enter', shiftKey: false });

    await waitFor(() => {
      expect(apiService.sendMessageStreamV2).toHaveBeenCalledWith(
        'new-session',
        [{ type: 'text', text: '触发发送前拒绝' }],
        expect.any(Object),
      );
    });

    rerender(
      <ChatV2
        sessionId="new-session"
        {...defaultProps}
        onCreateSession={onCreateSession}
      />
    );
    await waitFor(() => {
      expect(apiService.getSessionHistoryV2).toHaveBeenCalledWith('new-session');
    });

    await act(async () => {
      rejectSend(new Error('发送被拒绝'));
    });

    await waitFor(() => {
      expect(screen.getByLabelText('已选择本轮偏好')).toHaveTextContent('PDF 处理');
      expect(screen.getByLabelText('已选择本轮偏好')).toHaveTextContent('东方财富数据');
      expect(screen.getByPlaceholderText('输入指令...')).toHaveValue('触发发送前拒绝');
    });
  });

  it('欢迎页连续提交两次时只创建并发送一次', async () => {
    vi.mocked(apiService.getSessionHistoryV2).mockResolvedValue({
      rounds: [],
      session_id: 'new-session',
      total: 0,
    });
    vi.mocked(apiService.sendMessageStreamV2).mockResolvedValue(undefined);

    let resolveCreateSession!: (sessionId: string) => void;
    const onCreateSession = vi.fn(() => new Promise<string>((resolve) => {
      resolveCreateSession = resolve;
    }));

    render(
      <ChatV2
        sessionId=""
        {...defaultProps}
        onCreateSession={onCreateSession}
      />
    );

    const textarea = screen.getByPlaceholderText('输入你的问题，按 Enter 开始对话...');
    await act(async () => {
      fireEvent.change(textarea, { target: { value: '连续发送测试' } });
    });
    await act(async () => {
      fireEvent.keyDown(textarea, { key: 'Enter', shiftKey: false });
      fireEvent.keyDown(textarea, { key: 'Enter', shiftKey: false });
    });

    expect(onCreateSession).toHaveBeenCalledTimes(1);

    await act(async () => {
      resolveCreateSession('new-session');
    });

    await waitFor(() => {
      expect(apiService.sendMessageStreamV2).toHaveBeenCalledTimes(1);
      expect(apiService.sendMessageStreamV2).toHaveBeenCalledWith(
        'new-session',
        [{ type: 'text', text: '连续发送测试' }],
        expect.any(Object),
      );
    });
  });

  it('resume 失败后应恢复问题卡片以便重试', async () => {
    const waitingRounds: RoundData[] = [
      {
        round_id: 'round-waiting-1',
        user_message: '请帮我选数据库',
        final_response: '',
        steps: [],
        step_count: 0,
        status: 'waiting_interaction',
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
      rounds: waitingRounds,
      session_id: 'test-session',
      total: waitingRounds.length,
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

  it('连续 interaction_requested 应按 interaction id 重建问题卡本地状态', async () => {
    vi.mocked(apiService.getSessionHistoryV2).mockResolvedValue({
      rounds: [{
        round_id: 'round-waiting-identity',
        user_message: '连续提问',
        final_response: '',
        steps: [],
        step_count: 0,
        status: 'waiting_interaction',
        created_at: new Date().toISOString(),
        last_event_sequence: 7,
        interrupt: {
          id: 'interaction-1',
          reason: 'input_required',
          payload: { questions: [{ question: 'Old question?' }] },
        },
      }],
      session_id: 'test-session',
      total: 1,
    });

    render(<ChatV2 sessionId="test-session" {...defaultProps} />);
    const answer = await screen.findByLabelText('mock question answer');
    fireEvent.change(answer, { target: { value: 'answer from old card' } });
    expect(answer).toHaveValue('answer from old card');

    const subscribeCallbacks = vi.mocked(apiService.subscribeToRound).mock.calls[0][2] as any;
    await act(async () => {
      subscribeCallbacks.onCustomEvent('interaction_requested', {
        interactionId: 'interaction-2',
        runId: 'round-waiting-identity',
        kind: 'user_input',
        payload: { questions: [{ question: 'New question?' }] },
      });
    });

    expect(screen.getByText('Question: New question?')).toBeInTheDocument();
    expect(screen.getByLabelText('mock question answer')).toHaveValue('');
  });

  it('普通 ask_user 的问题键为 approval 时仍是用户回答而非工具审批控制', async () => {
    const waitingRounds: RoundData[] = [{
      round_id: 'round-ask-approval-key',
      user_message: '请确认审批字段',
      final_response: '',
      steps: [],
      step_count: 0,
      status: 'waiting_interaction',
      created_at: new Date().toISOString(),
      interrupt: {
        id: 'interrupt-approval-key',
        reason: 'input_required',
        payload: {
          questions: [{
            question: 'approval',
            header: '字段名称',
            options: [{ label: 'PostgreSQL', description: '普通回答' }],
          }],
        },
      },
    }];
    vi.mocked(apiService.getSessionHistoryV2).mockResolvedValue({
      rounds: waitingRounds,
      session_id: 'test-session',
      total: 1,
    });

    render(<ChatV2 sessionId="test-session" {...defaultProps} />);
    fireEvent.click(await screen.findByText('Submit Question'));

    await waitFor(() => {
      expect(apiService.resumeStream).toHaveBeenCalledWith(
        'test-session',
        'interrupt-approval-key',
        { approval: 'PostgreSQL' },
        expect.any(Object),
      );
      expect(screen.getAllByTestId('round')).toHaveLength(1);
      expect(screen.getByText('User: 请确认审批字段')).toBeInTheDocument();
    });
  });

  it('应渲染工具审批中断，并把精确审批结果提交到 resume 接口', async () => {
    const waitingRounds: RoundData[] = [
      {
        round_id: 'round-approval-1',
        user_message: '查询企业知识库',
        final_response: '',
        steps: [],
        step_count: 1,
        status: 'waiting_interaction',
        created_at: new Date().toISOString(),
        interrupt: {
          id: 'approval-1',
          reason: 'human_approval',
          payload: {
            kind: 'tool_approval',
            tool_ref: 'mcp:server-1:search',
            provider: 'mcp',
            source_type: 'official',
            server_id: 'server-1',
            server_name: '官方知识库',
            tool_name: 'search',
            tool_title: '知识检索',
            tool_description: '检索企业知识库',
            arguments_display: '{"query":"季度收入"}',
          },
        },
      },
    ];

    vi.mocked(apiService.getSessionHistoryV2).mockResolvedValue({
      rounds: waitingRounds,
      session_id: 'test-session',
      total: 1,
    });

    render(<ChatV2 sessionId="test-session" {...defaultProps} />);

    expect(await screen.findByText('工具执行需要确认')).toBeInTheDocument();
    expect(screen.getByText('官方 MCP · 官方知识库')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '暂时隐藏审批' })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: /允许本次/ }));

    await waitFor(() => {
      expect(apiService.resumeStream).toHaveBeenCalledWith(
        'test-session',
        'approval-1',
        { approval: 'allow_once' },
        expect.any(Object),
      );
      expect(screen.queryByText('User: Tool approval: allow_once')).not.toBeInTheDocument();
      expect(screen.getAllByTestId('round')).toHaveLength(1);
      expect(screen.getByText('User: 查询企业知识库')).toBeInTheDocument();
    });
  });

  it('resume 时同步抓取 Session dirty 草稿，但远端失败不阻止 continuation', async () => {
    sessionFilesControls.dirty = true;
    sessionFilesControls.save.mockResolvedValue({
      ok: false,
      stale: false,
      failedPaths: ['report.md'],
    });
    const question = '继续执行吗？';
    vi.mocked(apiService.getSessionHistoryV2).mockResolvedValue({
      rounds: [{
        round_id: 'round-resume-save-guard',
        user_message: '编辑后继续',
        final_response: '',
        steps: [],
        step_count: 1,
        status: 'waiting_interaction',
        created_at: new Date().toISOString(),
        interrupt: {
          id: 'resume-save-guard',
          reason: 'input_required',
          payload: {
            kind: 'ask_user',
            questions: [{ question, header: '确认', options: [] }],
          },
        },
      }],
      session_id: 'test-session',
      total: 1,
    });
    render(<ChatV2 sessionId="test-session" {...defaultProps} />);
    fireEvent.click(await screen.findByText('Submit Question'));

    await waitFor(() => expect(sessionFilesControls.save).toHaveBeenCalledWith(undefined));
    await waitFor(() => expect(apiService.resumeStream).toHaveBeenCalled());
    expect(screen.queryByText(/当前文件尚未成功保存到远端/)).not.toBeInTheDocument();
  });

  it('普通用户发送审批格式文本时不应被当作控制轮次', async () => {
    const literalApprovalMessage: RoundData = {
      round_id: 'round-literal-approval',
      user_message: 'Tool approval: deny',
      final_response: '这是普通聊天内容。',
      steps: [],
      step_count: 0,
      status: 'completed',
      created_at: '2026-07-16T10:00:00Z',
      completed_at: '2026-07-16T10:00:01Z',
    };
    vi.mocked(apiService.getSessionHistoryV2).mockResolvedValue({
      rounds: [literalApprovalMessage],
      session_id: 'test-session',
      total: 1,
    });

    render(<ChatV2 sessionId="test-session" {...defaultProps} />);

    expect(await screen.findByText('User: Tool approval: deny')).toBeInTheDocument();
    expect(screen.getAllByTestId('round')).toHaveLength(1);
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

  it('已打开 session 从无 slot 变为 init slot 时应启动 history 探测并锁定输入', async () => {
    vi.mocked(apiService.getSessionHistoryV2).mockResolvedValue({
      rounds: [],
      session_id: 'session-init',
      total: 0,
    });

    const { rerender } = render(
      <ChatV2
        sessionId="session-init"
        activeSlotSessionIds={new Set()}
        {...defaultProps}
      />
    );

    await waitFor(() => {
      expect(apiService.getSessionHistoryV2).toHaveBeenCalledTimes(1);
    });

    rerender(
      <ChatV2
        sessionId="session-init"
        activeSlotSessionIds={new Set(['session-init'])}
        {...defaultProps}
      />
    );

    await waitFor(() => {
      expect(apiService.getSessionHistoryV2).toHaveBeenCalledTimes(2);
      expect(defaultProps.onExecutionStart).toHaveBeenCalledWith('session-init');
    });

    await waitFor(() => {
      const textarea = screen.getByPlaceholderText('输入指令...') as HTMLTextAreaElement;
      expect(textarea).toBeDisabled();
    });
  });

  it('init-window active slot 轮询到 running round 后应订阅该轮次', async () => {
    const runningRound: RoundData = {
      round_id: 'round-ready',
      user_message: '初始化完成',
      final_response: '',
      steps: [],
      step_count: 0,
      status: 'running',
      created_at: new Date().toISOString(),
    };

    vi.mocked(apiService.getSessionHistoryV2)
      .mockResolvedValueOnce({
        rounds: [],
        session_id: 'session-init',
        total: 0,
      })
      .mockResolvedValueOnce({
        rounds: [runningRound],
        session_id: 'session-init',
        total: 1,
      });
    vi.mocked(apiService.getRunningSessions).mockResolvedValue({
      running_sessions: [{ session_id: 'session-init', round_id: null }],
    });

    render(
      <ChatV2
        sessionId="session-init"
        activeSlotSessionIds={new Set(['session-init'])}
        {...defaultProps}
      />
    );

    await waitFor(() => {
      expect(apiService.getSessionHistoryV2).toHaveBeenCalledTimes(1);
    });

    await waitFor(() => {
      expect(apiService.getSessionHistoryV2).toHaveBeenCalledTimes(2);
      expect(apiService.subscribeToRound).toHaveBeenCalledWith(
        'session-init',
        'round-ready',
        expect.any(Object),
        0,
      );
    }, { timeout: 3000 });
    expect(apiService.getRunningSessions).not.toHaveBeenCalled();
  });

  it('running history 订阅应从 last_event_sequence 后续接', async () => {
    const runningRound: RoundData = {
      round_id: 'round-sequenced',
      user_message: '运行中',
      final_response: '',
      steps: [],
      step_count: 0,
      status: 'running',
      created_at: new Date().toISOString(),
      last_event_sequence: 7,
    };

    vi.mocked(apiService.getSessionHistoryV2).mockResolvedValue({
      rounds: [runningRound],
      session_id: 'test-session',
      total: 1,
    });

    render(<ChatV2 sessionId="test-session" {...defaultProps} />);

    await waitFor(() => {
      expect(apiService.subscribeToRound).toHaveBeenCalledWith(
        'test-session',
        'round-sequenced',
        expect.any(Object),
        7,
      );
    });
  });

  it('已有本地运行轮次时历史校准不应重新进入阻塞加载态', async () => {
    let resolveHistory!: (value: {
      rounds: RoundData[];
      session_id: string;
      total: number;
    }) => void;
    vi.mocked(apiService.sendMessageStreamV2).mockImplementation(() => new Promise(() => {}));
    vi.mocked(apiService.getSessionHistoryV2).mockImplementation(() => new Promise((resolve) => {
      resolveHistory = resolve;
    }));

    render(
      <ChatRuntimeProvider>
        <RuntimeStreamOwnershipHarness />
      </ChatRuntimeProvider>,
    );

    fireEvent.click(screen.getByRole('button', { name: 'start direct run' }));
    await waitFor(() => {
      expect(screen.getByTestId('runtime-loading')).toHaveTextContent('false');
    });

    fireEvent.click(screen.getByRole('button', { name: 'reload history' }));
    await waitFor(() => {
      expect(apiService.getSessionHistoryV2).toHaveBeenCalledTimes(1);
    });
    expect(screen.getByTestId('runtime-loading')).toHaveTextContent('false');

    await act(async () => {
      resolveHistory({ rounds: [], session_id: 'test-session', total: 0 });
    });
  });

  describe('本轮推理等级', () => {
    const gradedModel = {
      id: 'graded-model',
      name: 'Graded Model',
      provider: 'openai',
      supports_thinking: true,
      supports_reasoning_control: true,
      thinking_mode: 'enabled' as const,
      reasoning_effort: 'high',
      default_reasoning_level: 'high',
      supported_reasoning_efforts: ['off', 'high', 'max'],
      supports_image: false,
      max_images: 0,
      supports_video: false,
      max_videos: 0,
      max_tokens: 8192,
      context_window: 128000,
      enabled: true,
      tags: [],
    };
    const switchModel = {
      ...gradedModel,
      id: 'switch-model',
      name: 'Switch Model',
      thinking_mode: 'disabled' as const,
      reasoning_effort: null,
      default_reasoning_level: 'off',
      supported_reasoning_efforts: ['off', 'on'],
    };
    const providerDefaultEffortModel = {
      ...gradedModel,
      id: 'provider-default-effort-model',
      name: 'Provider Default Effort Model',
      thinking_mode: 'provider_default' as const,
      reasoning_effort: 'high',
      default_reasoning_level: 'high',
      supported_reasoning_efforts: ['high', 'max'],
    };

    beforeEach(() => {
      vi.mocked(apiService.getSessionHistoryV2).mockResolvedValue({
        rounds: [],
        session_id: 'test-session',
        total: 0,
      });
      vi.mocked(apiService.sendMessageStreamV2).mockResolvedValue(undefined);
    });

    it('切换模型后按新模型目录默认值重置推理选择', async () => {
      const { rerender } = render(
        <ChatV2
          sessionId="test-session"
          {...defaultProps}
          selectedModelId="graded-model"
          availableModels={[gradedModel, switchModel]}
        />,
      );

      expect(await screen.findByRole('button', { name: /推理等级 High/ })).toBeInTheDocument();

      rerender(
        <ChatV2
          sessionId="test-session"
          {...defaultProps}
          selectedModelId="switch-model"
          availableModels={[gradedModel, switchModel]}
        />,
      );

      expect(await screen.findByRole('button', { name: /推理等级 Off/ })).toBeInTheDocument();
    });

    it('发送时冻结推理快照，之后修改只影响下一轮', async () => {
      render(
        <ChatV2
          sessionId="test-session"
          {...defaultProps}
          selectedModelId="graded-model"
          availableModels={[gradedModel]}
        />,
      );

      const textarea = await screen.findByPlaceholderText('输入指令...');
      fireEvent.change(textarea, { target: { value: '第一轮' } });
      fireEvent.keyDown(textarea, { key: 'Enter', shiftKey: false });

      await waitFor(() => expect(startSendStream).toHaveBeenCalledTimes(1));
      expect(vi.mocked(startSendStream).mock.calls[0][0].reasoning).toEqual({
        mode: 'enabled',
        effort: 'high',
      });

      // 会话已锁定模型，触发器直接展开推理等级面板。
      fireEvent.click(screen.getByRole('button', { name: /推理等级 High/ }));
      fireEvent.click(await screen.findByRole('menuitemradio', { name: 'Max' }));

      expect(startSendStream).toHaveBeenCalledTimes(1);
      expect(vi.mocked(startSendStream).mock.calls[0][0].reasoning).toEqual({
        mode: 'enabled',
        effort: 'high',
      });
      expect(screen.getByRole('button', { name: /推理等级 Max/ })).toBeInTheDocument();
    });

    it('目录默认强度不应把 provider_default 推断成 enabled', async () => {
      render(
        <ChatV2
          sessionId="test-session"
          {...defaultProps}
          selectedModelId="provider-default-effort-model"
          availableModels={[providerDefaultEffortModel]}
        />,
      );

      const textarea = await screen.findByPlaceholderText('输入指令...');
      fireEvent.change(textarea, { target: { value: '沿用供应商默认开关' } });
      fireEvent.keyDown(textarea, { key: 'Enter', shiftKey: false });

      await waitFor(() => expect(startSendStream).toHaveBeenCalledTimes(1));
      expect(vi.mocked(startSendStream).mock.calls[0][0].reasoning).toEqual({
        mode: 'provider_default',
        effort: 'high',
      });
    });

    it('选择 Off 后发送 disabled 且不带强度', async () => {
      render(
        <ChatV2
          sessionId="test-session"
          {...defaultProps}
          selectedModelId="graded-model"
          availableModels={[gradedModel]}
        />,
      );

      const textarea = await screen.findByPlaceholderText('输入指令...');
      fireEvent.change(textarea, { target: { value: '关闭思考' } });
      textarea.focus();

      fireEvent.click(screen.getByRole('button', { name: /推理等级 High/ }));
      fireEvent.click(await screen.findByRole('menuitemradio', { name: 'Off' }));

      expect(textarea).toHaveFocus();
      fireEvent.keyDown(document.activeElement as HTMLElement, { key: 'Enter', shiftKey: false });

      await waitFor(() => expect(startSendStream).toHaveBeenCalledTimes(1));
      expect(vi.mocked(startSendStream).mock.calls[0][0].reasoning).toEqual({
        mode: 'disabled',
        effort: null,
      });
    });

    it('同一模型的不同会话分别保存本轮推理选择', async () => {
      vi.mocked(apiService.getSessionHistoryV2).mockImplementation(async (sessionId) => ({
        rounds: [],
        session_id: sessionId,
        total: 0,
      }));
      const { rerender } = render(
        <ChatV2
          sessionId="session-a"
          {...defaultProps}
          selectedModelId="graded-model"
          availableModels={[gradedModel]}
        />,
      );

      fireEvent.click(await screen.findByRole('button', { name: /推理等级 High/ }));
      fireEvent.click(await screen.findByRole('menuitemradio', { name: 'Off' }));

      rerender(
        <ChatV2
          sessionId="session-b"
          {...defaultProps}
          selectedModelId="graded-model"
          availableModels={[gradedModel]}
        />,
      );
      expect(await screen.findByRole('button', { name: /推理等级 High/ })).toBeInTheDocument();
      fireEvent.click(screen.getByRole('button', { name: /推理等级 High/ }));
      fireEvent.click(await screen.findByRole('menuitemradio', { name: 'Max' }));

      rerender(
        <ChatV2
          sessionId="session-a"
          {...defaultProps}
          selectedModelId="graded-model"
          availableModels={[gradedModel]}
        />,
      );
      expect(await screen.findByRole('button', { name: /推理等级 Off/ })).toBeInTheDocument();
      const sessionATextarea = screen.getByPlaceholderText('输入指令...');
      fireEvent.change(sessionATextarea, { target: { value: '会话 A' } });
      fireEvent.keyDown(sessionATextarea, { key: 'Enter', shiftKey: false });
      await waitFor(() => expect(startSendStream).toHaveBeenCalledTimes(1));
      expect(vi.mocked(startSendStream).mock.calls[0][0].reasoning).toEqual({
        mode: 'disabled',
        effort: null,
      });

      rerender(
        <ChatV2
          sessionId="session-b"
          {...defaultProps}
          selectedModelId="graded-model"
          availableModels={[gradedModel]}
        />,
      );
      expect(await screen.findByRole('button', { name: /推理等级 Max/ })).toBeInTheDocument();
      const sessionBTextarea = screen.getByPlaceholderText('输入指令...');
      fireEvent.change(sessionBTextarea, { target: { value: '会话 B' } });
      fireEvent.keyDown(sessionBTextarea, { key: 'Enter', shiftKey: false });
      await waitFor(() => expect(startSendStream).toHaveBeenCalledTimes(2));
      expect(vi.mocked(startSendStream).mock.calls[1][0].reasoning).toEqual({
        mode: 'enabled',
        effort: 'max',
      });
    });

    it('欢迎页的推理选择随 draft 迁移到新会话', async () => {
      vi.mocked(apiService.getSessionHistoryV2).mockImplementation(async (sessionId) => ({
        rounds: [],
        session_id: sessionId,
        total: 0,
      }));
      vi.mocked(apiService.uploadFile).mockResolvedValue({
        name: 'reasoning.txt',
        path: 'reasoning.txt',
        size: 9,
        modified: new Date().toISOString(),
        type: 'text/plain',
      });
      const onCreateSession = vi.fn().mockResolvedValue('new-session');
      const { container, rerender } = render(
        <ChatV2
          sessionId=""
          {...defaultProps}
          selectedModelId="graded-model"
          availableModels={[gradedModel]}
          onCreateSession={onCreateSession}
        />,
      );

      fireEvent.click(await screen.findByRole('button', { name: /推理等级 High/ }));
      fireEvent.click(screen.getByRole('menuitem', { name: /推理等级 High/ }));
      fireEvent.click(await screen.findByRole('menuitemradio', { name: 'Off' }));

      const fileInput = container.querySelector('input[type="file"]') as HTMLInputElement;
      fireEvent.change(fileInput, {
        target: { files: [new File(['reasoning'], 'reasoning.txt', { type: 'text/plain' })] },
      });
      await waitFor(() => {
        expect(apiService.uploadFile).toHaveBeenCalledWith('new-session', expect.any(File));
      });

      rerender(
        <ChatV2
          sessionId="new-session"
          {...defaultProps}
          selectedModelId="graded-model"
          availableModels={[gradedModel]}
          onCreateSession={onCreateSession}
        />,
      );
      expect(await screen.findByRole('button', { name: /推理等级 Off/ })).toBeInTheDocument();
    });

    it('会话标题栏始终显示动态聊天面板按钮', async () => {
      const { container } = render(
        <ChatV2
          sessionId="test-session"
          {...defaultProps}
          selectedModelId="graded-model"
          availableModels={[gradedModel]}
        />,
      );

      const header = container.querySelector('header')!;
      expect(header).toHaveClass('h-14', 'shrink-0', 'border-b', 'border-claude-border');
      expect(header.querySelectorAll('.w-\\[88px\\]')).toHaveLength(0);
      expect(await screen.findByRole('button', { name: '查看文件' })).toHaveClass('h-6', 'w-6');
      expect(screen.getByRole('button', { name: '收起面板' })).toHaveClass('h-6', 'w-6');
      fireEvent.click(screen.getByRole('button', { name: '查看文件' }));
      expect(await screen.findByRole('button', { name: '展开面板' })).toHaveClass('h-6', 'w-6');
      expect(header.querySelector('.ml-auto')).toBeInTheDocument();
    });
  });

  it('历史刷新看到同一 running round 时不应为 direct run 再开一条订阅', async () => {
    let directCallbacks: any;
    let finishDirect!: () => void;
    let resolveHistory!: (value: {
      rounds: RoundData[];
      session_id: string;
      total: number;
    }) => void;
    vi.mocked(apiService.sendMessageStreamV2).mockImplementation(
      async (_sessionId, _content, callbacks) => {
        directCallbacks = callbacks;
        callbacks.onRunStarted?.('test-session', 'round-direct');
        await new Promise<void>((resolve) => {
          finishDirect = resolve;
        });
      },
    );
    vi.mocked(apiService.getSessionHistoryV2).mockImplementation(() => (
      new Promise((resolve) => {
        resolveHistory = resolve;
      })
    ));
    const runningHistory = {
      rounds: [{
        round_id: 'round-direct',
        user_message: 'hello',
        final_response: '',
        steps: [],
        step_count: 0,
        status: 'running',
        created_at: new Date().toISOString(),
        last_event_sequence: 1,
      }],
      session_id: 'test-session',
      total: 1,
    };

    render(
      <ChatRuntimeProvider>
        <RuntimeStreamOwnershipHarness />
      </ChatRuntimeProvider>,
    );

    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'reload history' }));
      fireEvent.click(screen.getByRole('button', { name: 'start direct run' }));
      resolveHistory(runningHistory);
      await Promise.resolve();
    });

    expect(apiService.sendMessageStreamV2).toHaveBeenCalledTimes(1);
    expect(apiService.getSessionHistoryV2).toHaveBeenCalledTimes(1);
    expect(apiService.subscribeToRound).not.toHaveBeenCalled();

    await act(async () => {
      directCallbacks.onRunFinished(
        'test-session',
        'round-direct',
        { finalResponse: 'done' },
        'success',
        undefined,
      );
      finishDirect();
    });
  });

  it('running history 订阅瞬断重试期间不应显示错误横幅', async () => {
    const runningRound: RoundData = {
      round_id: 'round-retry',
      user_message: '运行中',
      final_response: '',
      steps: [],
      step_count: 0,
      status: 'running',
      created_at: new Date().toISOString(),
      last_event_sequence: 7,
    };

    let subscribeCalls = 0;
    vi.mocked(apiService.subscribeToRound).mockImplementation(() => {
      subscribeCalls += 1;
      return {
        abort: vi.fn(),
        promise: subscribeCalls === 1
          ? Promise.reject(new Error('SSE_STREAM_CLOSED'))
          : new Promise(() => {}),
      } as any;
    });

    vi.mocked(apiService.getSessionHistoryV2).mockResolvedValue({
      rounds: [runningRound],
      session_id: 'test-session',
      total: 1,
    });

    render(<ChatV2 sessionId="test-session" {...defaultProps} />);

    await waitFor(() => {
      expect(apiService.subscribeToRound).toHaveBeenCalledTimes(2);
    }, { timeout: 2500 });

    expect(screen.queryByText(/SSE_STREAM_CLOSED|订阅连接已断开/)).not.toBeInTheDocument();
  });

  it('running history 收到终态时应清理临时 assistant_content', async () => {
    const runningRound: RoundData = {
      round_id: 'round-live-final',
      user_message: '运行中',
      final_response: '',
      steps: [
        {
          step_number: 1,
          thinking: '',
          assistant_content: '临时正文',
          tool_calls: [],
          tool_results: [],
          status: 'running',
        },
      ],
      step_count: 1,
      status: 'running',
      created_at: new Date().toISOString(),
      last_event_sequence: 4,
    };

    let subscribeCallbacks: any = null;
    vi.mocked(apiService.subscribeToRound).mockImplementation((_sid, _rid, callbacks: any) => {
      subscribeCallbacks = callbacks;
      return {
        abort: vi.fn(),
        promise: new Promise(() => {}),
      } as any;
    });

    vi.mocked(apiService.getSessionHistoryV2).mockResolvedValue({
      rounds: [runningRound],
      session_id: 'test-session',
      total: 1,
    });

    render(<ChatV2 sessionId="test-session" {...defaultProps} />);

    await waitFor(() => {
      expect(apiService.subscribeToRound).toHaveBeenCalled();
    });

    await act(async () => {
      subscribeCallbacks.onRunFinished(
        'test-session',
        'round-live-final',
        { finalResponse: '最终结果' },
        'success',
        undefined,
      );
    });

    await waitFor(() => {
      expect(screen.getByTestId('round')).toHaveAttribute('data-assistant', '最终结果');
    });

    const steps = JSON.parse(screen.getByTestId('round').getAttribute('data-steps') || '[]');
    expect(steps[0].assistant_content).toBe('');
    expect(steps[0].status).toBe('completed');
  });

  it('StrictMode 下并发历史加载只应保留最新 running 订阅', async () => {
    const runningRounds: RoundData[] = [
      {
        round_id: 'round-strict-running',
        user_message: '运行中',
        final_response: '',
        steps: [],
        step_count: 0,
        status: 'running',
        created_at: new Date().toISOString(),
      },
    ];
    const historyResponse = {
      rounds: runningRounds,
      session_id: 'test-session',
      total: runningRounds.length,
    };

    let resolveFirstHistory!: (value: typeof historyResponse) => void;
    let resolveLatestHistory!: (value: typeof historyResponse) => void;
    const firstHistory = new Promise<typeof historyResponse>((resolve) => {
      resolveFirstHistory = resolve;
    });
    const latestHistory = new Promise<typeof historyResponse>((resolve) => {
      resolveLatestHistory = resolve;
    });

    let historyCallCount = 0;
    vi.mocked(apiService.getSessionHistoryV2).mockImplementation(() => {
      historyCallCount += 1;
      return historyCallCount === 1 ? firstHistory : latestHistory;
    });
    vi.mocked(apiService.subscribeToRound).mockReturnValue({
      abort: vi.fn(),
      promise: new Promise(() => {}),
    } as any);

    render(
      <StrictMode>
        <ChatV2
          sessionId="test-session"
          {...defaultProps}
        />
      </StrictMode>
    );

    await waitFor(() => {
      expect(apiService.getSessionHistoryV2).toHaveBeenCalledTimes(2);
    });

    await act(async () => {
      resolveLatestHistory(historyResponse);
      await latestHistory;
    });

    await waitFor(() => {
      expect(apiService.subscribeToRound).toHaveBeenCalledTimes(1);
    });

    await act(async () => {
      resolveFirstHistory(historyResponse);
      await firstHistory;
    });

    expect(apiService.subscribeToRound).toHaveBeenCalledTimes(1);
    expect(apiService.subscribeToRound).toHaveBeenCalledWith(
      'test-session',
      'round-strict-running',
      expect.any(Object),
      0,
    );
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

    let resolveAbort: () => void = () => {};
    vi.mocked(apiService.abortChat).mockImplementation(() => new Promise((resolve) => {
      resolveAbort = () => resolve(ABORT_RESPONSE);
    }));

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

    // UI 应该在 abort HTTP 返回前立即更新 — 不再显示停止按钮，输入框可用
    expect(screen.queryByTitle('停止生成')).not.toBeInTheDocument();
    expect(screen.getByTestId('round')).toHaveAttribute('data-status', 'cancelled');

    const textarea = screen.getByPlaceholderText('输入指令...') as HTMLTextAreaElement;
    expect(textarea).not.toBeDisabled();

    await act(async () => {
      fireEvent.change(textarea, { target: { value: '马上问新问题' } });
    });

    await act(async () => {
      fireEvent.keyDown(textarea, { key: 'Enter', shiftKey: false });
    });

    expect(apiService.sendMessageStreamV2).not.toHaveBeenCalled();

    // onExecutionEnd 应该被调用
    expect(defaultProps.onExecutionEnd).toHaveBeenCalled();

    await act(async () => {
      resolveAbort();
    });

    // 停止成功后不再重复展示后端的保守副作用提示。
    expect(screen.queryByTestId('runtime-warning')).not.toBeInTheDocument();
    expect(screen.queryByText(ABORT_WARNING)).not.toBeInTheDocument();

    await act(async () => {
      fireEvent.keyDown(textarea, { key: 'Enter', shiftKey: false });
    });

    expect(apiService.sendMessageStreamV2).toHaveBeenCalledWith(
      'test-session',
      [{ type: 'text', text: '马上问新问题' }],
      expect.any(Object)
    );
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

    expect(screen.getByTestId('round')).toHaveAttribute('data-status', 'cancelled');
  });

  it('只收到 RUN_FINISHED 时也应收敛临时 round 并展示最终结果', async () => {
    vi.mocked(apiService.getSessionHistoryV2).mockResolvedValue({
      rounds: [],
      session_id: 'test-session',
      total: 0,
    });

    vi.mocked(apiService.sendMessageStreamV2).mockImplementation(async (_sid, _content, callbacks) => {
      callbacks.onStreamAccepted?.();
      callbacks.onRunFinished?.(
        'test-session',
        'restored-run-1',
        { finalResponse: '最终结果已生成' },
        'success',
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
      fireEvent.change(textarea, { target: { value: '触发最终结果恢复' } });
    });

    await act(async () => {
      fireEvent.keyDown(textarea, { key: 'Enter', shiftKey: false });
    });

    await waitFor(() => {
      expect(screen.getByText('Round: restored-run-1')).toBeInTheDocument();
      expect(screen.getByTestId('round')).toHaveAttribute('data-assistant', '最终结果已生成');
    });
    expect(defaultProps.onExecutionEnd).toHaveBeenCalledWith('test-session');
  });

  it('abort 时应清理残留的 pendingInterrupt（QuestionCard 不应残留）', async () => {
    const waitingRounds: RoundData[] = [
      {
        round_id: 'round-int-1',
        user_message: '分析一下',
        final_response: '',
        steps: [],
        step_count: 1,
        status: 'waiting_interaction',
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
      rounds: waitingRounds,
      session_id: 'test-session',
      total: 1,
    });

    // 模拟 subscribeToRound 立即完成（不需要真正订阅）
    vi.mocked(apiService.subscribeToRound).mockReturnValue({
      abort: vi.fn(),
      promise: Promise.resolve(),
    } as any);

    vi.mocked(apiService.abortChat).mockResolvedValue(ABORT_RESPONSE);

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

    await act(async () => {
      fireEvent.click(screen.getByTitle('停止生成'));
    });

    await waitFor(() => {
      expect(screen.queryByTestId('question-card')).not.toBeInTheDocument();
      expect(screen.getByTestId('round')).toHaveAttribute('data-status', 'cancelled');
    });
  });

  it('waiting interaction 不提供仅本地关闭入口，避免隐藏后无法继续', async () => {
    const waitingRounds: RoundData[] = [
      {
        round_id: 'round-dismiss-1',
        user_message: '分析一下',
        final_response: '',
        steps: [],
        step_count: 1,
        status: 'waiting_interaction',
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
      rounds: waitingRounds,
      session_id: 'test-session',
      total: 1,
    });

    render(<ChatV2 sessionId="test-session" {...defaultProps} />);

    // 等待 QuestionCard 出现
    await waitFor(() => {
      expect(screen.getByTestId('question-card')).toBeInTheDocument();
    });

    expect(screen.queryByTestId('question-card-dismiss')).not.toBeInTheDocument();
    expect(screen.getByTestId('question-card')).toBeInTheDocument();
    expect(apiService.resumeStream).not.toHaveBeenCalled();
    expect(apiService.abortChat).not.toHaveBeenCalled();
  });

  it('abort 请求失败前先本地停止，失败后重新同步运行态', async () => {
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

    let rejectAbort: (reason?: unknown) => void = () => {};
    vi.mocked(apiService.abortChat).mockImplementation(() => new Promise((_resolve, reject) => {
      rejectAbort = reject;
    }));

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

    // abort HTTP 尚未返回时，UI 已经先本地停止。
    expect(screen.queryByTitle('停止生成')).not.toBeInTheDocument();

    const textarea = screen.getByPlaceholderText('输入指令...') as HTMLTextAreaElement;
    expect(textarea).not.toBeDisabled();
    expect(defaultProps.onExecutionEnd).toHaveBeenCalledWith('test-session');

    await act(async () => {
      rejectAbort(new Error('Network Error'));
    });

    // abort 失败后重新拉取历史；若后端仍是 running，则恢复运行态并提示错误。
    await waitFor(() => {
      expect(screen.getByTitle('停止生成')).toBeInTheDocument();
    });
    expect(textarea).toBeDisabled();
    expect(screen.getByText('停止请求失败，后端任务可能仍在运行')).toBeInTheDocument();
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
        undefined,
        { sequence: 9 },
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

    // sendMessageStreamV2：直接调用 onRunError(USER_BUSY)，不调用 onStreamAccepted/onRunStarted
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

  it('后端仅接受 SSE 时不应在 RUN_STARTED 前标记执行态', async () => {
    vi.mocked(apiService.getSessionHistoryV2).mockResolvedValue({
      rounds: [],
      session_id: 'test-session',
      total: 0,
    });

    vi.mocked(apiService.sendMessageStreamV2).mockImplementation(async (_sid, _content, callbacks) => {
      callbacks.onStreamAccepted?.();
    });

    render(
      <ChatV2
        sessionId="test-session"
        {...defaultProps}
      />
    );

    const textarea = screen.getByPlaceholderText('输入指令...') as HTMLTextAreaElement;

    await act(async () => {
      fireEvent.change(textarea, { target: { value: '测试初始化窗口标记' } });
    });

    await act(async () => {
      fireEvent.keyDown(textarea, { key: 'Enter', shiftKey: false });
    });

    expect(defaultProps.onExecutionStart).not.toHaveBeenCalled();
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
