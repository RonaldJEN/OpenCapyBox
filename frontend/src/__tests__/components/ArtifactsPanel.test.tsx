import { createRef, forwardRef, useEffect, useImperativeHandle, useRef } from 'react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { act, render, screen, fireEvent, waitFor } from '../utils/test-utils';
import { ArtifactsPanel, type ArtifactsPanelHandle } from '../../components/ArtifactsPanel';
import { apiService } from '../../services/api';
import { emitWorkspaceMutation, resetWorkspaceEventsForTests } from '../../services/workspaceEvents';
import { FileInfo } from '../../types';

let previewFileEffects: FileInfo[] = [];

vi.mock('../../components/FilePreview', () => ({
  FilePreview: forwardRef(function FilePreviewMock({
    file,
    sessionId,
    ownerEpoch = 0,
    inline,
    onClose,
    onDirtyChange,
    onSavingChange,
    onSaveFailure,
    saveRequestNonce,
    canSaveToWorkspace,
  }: {
    file?: FileInfo;
    sessionId: string;
    ownerEpoch?: number;
    inline?: boolean;
    onClose: () => void;
    onDirtyChange?: (dirty: boolean) => void;
    onSavingChange?: (saving: boolean) => void;
    onSaveFailure?: () => void;
    saveRequestNonce?: number;
    canSaveToWorkspace?: boolean;
  }, ref) {
    const dirtyRef = useRef(false);
    useImperativeHandle(ref, () => ({
      ownerSessionId: sessionId,
      ownerEpoch,
      path: file?.path || '',
      isDirty: (expectedOwner: { ownerSessionId: string; ownerEpoch: number }) => (
        dirtyRef.current
        && expectedOwner.ownerSessionId === sessionId
        && expectedOwner.ownerEpoch === ownerEpoch
      ),
      saveDirty: async (expectedOwner: { ownerSessionId: string; ownerEpoch: number }) => {
        onSavingChange?.(true);
        dirtyRef.current = false;
        onDirtyChange?.(false);
        onSavingChange?.(false);
        return {
          ...expectedOwner,
          path: file?.path || '',
          ok: expectedOwner.ownerSessionId === sessionId && expectedOwner.ownerEpoch === ownerEpoch,
          stale: expectedOwner.ownerSessionId !== sessionId || expectedOwner.ownerEpoch !== ownerEpoch,
        };
      },
    }), [file?.path, onDirtyChange, onSavingChange, ownerEpoch, sessionId]);
    useEffect(() => {
      if (file) previewFileEffects.push(file);
    }, [file]);
    return (
      <div data-testid="file-preview-inline-mock" data-inline={String(inline)} data-save-request-nonce={saveRequestNonce} data-can-save-to-workspace={String(canSaveToWorkspace)}>
        <span>Inline Preview: {file?.name}</span>
        <button onClick={onClose}>Close Inline Preview</button>
        <button onClick={() => { dirtyRef.current = true; onDirtyChange?.(true); }}>Mark Markdown Dirty</button>
        <button onClick={() => onSavingChange?.(true)}>Start Markdown Save</button>
        <button onClick={() => { dirtyRef.current = false; onDirtyChange?.(false); onSavingChange?.(false); }}>Finish Markdown Save</button>
        <button onClick={() => { onSaveFailure?.(); onSavingChange?.(false); }}>Fail Markdown Save</button>
      </div>
    );
  }),
}));

// Mock apiService
vi.mock('../../services/api', () => ({
  apiService: {
    getSessionFiles: vi.fn(),
    downloadFile: vi.fn(),
  },
}));

describe('ArtifactsPanel 组件', () => {
  const mockFiles: FileInfo[] = [
    {
      name: 'report.pdf',
      path: '/workspace/report.pdf',
      size: 1024 * 100,
      type: 'application/pdf',
      modified: new Date().toISOString(),
      is_directory: false,
    },
    {
      name: 'data.xlsx',
      path: '/workspace/data.xlsx',
      size: 1024 * 50,
      type: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
      modified: new Date().toISOString(),
      is_directory: false,
    },
    {
      name: 'script.py',
      path: '/workspace/script.py',
      size: 1024 * 5,
      type: 'text/x-python',
      modified: new Date().toISOString(),
      is_directory: false,
    },
  ];

  beforeEach(() => {
    vi.clearAllMocks();
    previewFileEffects = [];
    resetWorkspaceEventsForTests();
    vi.mocked(apiService.getSessionFiles).mockResolvedValue({
      files: mockFiles,
      total: mockFiles.length,
    });
  });

  it('面板关闭时不应该加载文件', () => {
    render(
      <ArtifactsPanel
        sessionId="test-session"
        isOpen={false}
        onClose={vi.fn()}
      />,
    );

    expect(apiService.getSessionFiles).not.toHaveBeenCalled();
  });

  it('面板打开时应该加载文件列表', async () => {
    render(
      <ArtifactsPanel
        sessionId="test-session"
        isOpen
        onClose={vi.fn()}
      />,
    );

    await waitFor(() => {
      expect(apiService.getSessionFiles).toHaveBeenCalledWith('test-session', undefined);
    });
  });

  it('应该显示文件列表', async () => {
    render(
      <ArtifactsPanel
        sessionId="test-session"
        isOpen
        onClose={vi.fn()}
      />,
    );

    await waitFor(() => {
      expect(screen.getByText('report.pdf')).toBeInTheDocument();
      expect(screen.getByText('data.xlsx')).toBeInTheDocument();
      expect(screen.getByText('script.py')).toBeInTheDocument();
    });
  });

  it('应该隐藏顶部标题并显示路径导航', () => {
    render(
      <ArtifactsPanel
        sessionId="test-session"
        isOpen
        onClose={vi.fn()}
      />,
    );

    expect(screen.queryByText('会话资源管理')).not.toBeInTheDocument();
    expect(screen.queryByText('文件预览')).not.toBeInTheDocument();
    expect(screen.getAllByTitle('~/sessions/test-session').length).toBeGreaterThan(0);
  });

  it('永久删除会关闭对应 workspace snapshot、清理陈旧目录并禁止重新导入', async () => {
    const snapshot: FileInfo = {
      name: 'deleted.md',
      path: '.workspace-snapshots/deleted-entry/version-1/deleted.md',
      source: 'session',
      session_id: 'test-session',
      size: 65,
      type: 'md',
      modified: '2026-09-01T08:00:00Z',
      content_mode: 'captured',
    };
    vi.mocked(apiService.getSessionFiles).mockImplementation(async (_sessionId, path) => ({
      files: path ? [snapshot] : [],
      total: path ? 1 : 0,
    }));

    render(
      <ArtifactsPanel
        sessionId="test-session"
        isOpen
        onClose={vi.fn()}
        targetFile={snapshot}
        targetFileNonce={1}
      />,
    );

    expect(await screen.findByText('Inline Preview: deleted.md')).toBeInTheDocument();
    expect(screen.getByTestId('file-preview-inline-mock')).toHaveAttribute(
      'data-can-save-to-workspace',
      'false',
    );

    act(() => {
      emitWorkspaceMutation({
        operation: 'DELETED',
        entryId: 'deleted-entry',
        affectedEntryIds: ['deleted-entry'],
        tombstone: true,
      });
    });

    await waitFor(() => {
      expect(screen.queryByText('Inline Preview: deleted.md')).not.toBeInTheDocument();
      expect(apiService.getSessionFiles).toHaveBeenCalledWith('test-session', undefined);
    });
  });

  it('工作台顶栏应该与聊天顶栏共享 56px 基准线', () => {
    render(
      <ArtifactsPanel
        sessionId="test-session"
        isOpen
        onClose={vi.fn()}
        variant="workspace"
      />,
    );

    expect(screen.getByTestId('session-files-toolbar')).toHaveClass(
      'h-14',
      'shrink-0',
      'border-b',
      'border-claude-border',
    );
  });

  it('点击关闭按钮应该调用 onClose', () => {
    const mockOnClose = vi.fn();

    render(
      <ArtifactsPanel
        sessionId="test-session"
        isOpen
        onClose={mockOnClose}
      />,
    );

    fireEvent.click(screen.getByRole('button', { name: '收起文件' }));

    expect(mockOnClose).toHaveBeenCalled();
  });

  it('点击文件应该在面板内直接预览', async () => {
    render(
      <ArtifactsPanel
        sessionId="test-session"
        isOpen
        onClose={vi.fn()}
      />,
    );

    await waitFor(() => {
      expect(screen.getByText('report.pdf')).toBeInTheDocument();
    });

    fireEvent.click(screen.getByText('report.pdf'));

    await waitFor(() => {
      expect(screen.getByTestId('file-preview-inline-mock')).toBeInTheDocument();
      expect(screen.getByText('Inline Preview: report.pdf')).toBeInTheDocument();
      expect(screen.getByTestId('file-preview-inline-mock')).toHaveAttribute('data-inline', 'true');
    });
  });

  it('外部目标文件应该直接打开面板内预览并加载父目录', async () => {
    const targetFile: FileInfo = {
      name: 'summary.md',
      path: 'reports/summary.md',
      size: 0,
      type: 'md',
      modified: '',
      is_directory: false,
    };
    const hydratedFile: FileInfo = {
      ...targetFile,
      size: 2048,
      modified: new Date().toISOString(),
    };
    vi.mocked(apiService.getSessionFiles).mockImplementation(async (_sessionId, path) => {
      if (path === 'reports') {
        return { files: [hydratedFile], total: 1 };
      }
      return { files: mockFiles, total: mockFiles.length };
    });

    render(
      <ArtifactsPanel
        sessionId="test-session"
        isOpen
        onClose={vi.fn()}
        targetFile={targetFile}
        targetFileNonce={1}
      />,
    );

    expect(screen.getByTestId('file-preview-inline-mock')).toBeInTheDocument();
    expect(screen.getByText('Inline Preview: summary.md')).toBeInTheDocument();

    await waitFor(() => {
      expect(apiService.getSessionFiles).toHaveBeenCalledWith('test-session', 'reports');
    });
    expect(apiService.getSessionFiles).toHaveBeenCalledTimes(1);
    expect(apiService.getSessionFiles).not.toHaveBeenCalledWith('test-session', undefined);
  });

  it('相同版本的目录元数据不应该重新初始化目标预览', async () => {
    const targetFile: FileInfo = {
      name: 'instant.html',
      path: 'reports/instant.html',
      size: 4096,
      type: 'html',
      modified: '2026-08-26T06:00:00Z',
      is_directory: false,
      session_id: 'test-session',
    };
    vi.mocked(apiService.getSessionFiles).mockResolvedValue({
      files: [{ ...targetFile, session_id: undefined }],
      total: 1,
    });

    render(
      <ArtifactsPanel
        sessionId="test-session"
        isOpen
        onClose={vi.fn()}
        targetFile={targetFile}
        targetFileNonce={1}
      />,
    );

    await waitFor(() => {
      expect(apiService.getSessionFiles).toHaveBeenCalledTimes(1);
    });
    await waitFor(() => {
      expect(previewFileEffects).toHaveLength(1);
    });
    expect(previewFileEffects[0]).toMatchObject(targetFile);
  });

  it('关闭内联预览后应该返回文件列表', async () => {
    render(
      <ArtifactsPanel
        sessionId="test-session"
        isOpen
        onClose={vi.fn()}
      />,
    );

    await waitFor(() => {
      expect(screen.getByText('report.pdf')).toBeInTheDocument();
    });

    fireEvent.click(screen.getByText('report.pdf'));

    await waitFor(() => {
      expect(screen.getByTestId('file-preview-inline-mock')).toBeInTheDocument();
    });

    fireEvent.click(screen.getByRole('button', { name: 'Close Inline Preview' }));

    await waitFor(() => {
      expect(screen.queryByTestId('file-preview-inline-mock')).not.toBeInTheDocument();
      expect(screen.getByText('data.xlsx')).toBeInTheDocument();
      expect(screen.getByText('script.py')).toBeInTheDocument();
    });
  });

  it('应该保留多个文件标签并允许在标签间切换', async () => {
    render(
      <ArtifactsPanel
        sessionId="test-session"
        isOpen
        onClose={vi.fn()}
        variant="workspace"
      />,
    );

    fireEvent.click(await screen.findByText('report.pdf'));
    expect(await screen.findByText('Inline Preview: report.pdf')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '查看目录' }));
    fireEvent.click(await screen.findByText('data.xlsx'));

    expect(await screen.findByText('Inline Preview: data.xlsx')).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: 'report.pdf' })).toHaveAttribute('aria-selected', 'false');
    expect(screen.getByRole('tab', { name: 'data.xlsx' })).toHaveAttribute('aria-selected', 'true');

    fireEvent.click(screen.getByRole('tab', { name: 'report.pdf' }));
    expect(await screen.findByText('Inline Preview: report.pdf')).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: 'report.pdf' })).toHaveAttribute('aria-selected', 'true');
  });

  it('未保存的 Markdown 标签抓取草稿后立即关闭', async () => {
    vi.mocked(apiService.getSessionFiles).mockResolvedValueOnce({
      files: [{
        name: 'notes.md',
        path: 'notes.md',
        size: 64,
        type: 'md',
        modified: '2026-08-26T02:00:00Z',
        is_directory: false,
      }],
      total: 1,
    });
    render(
      <ArtifactsPanel
        sessionId="test-session"
        isOpen
        onClose={vi.fn()}
        variant="workspace"
      />,
    );

    fireEvent.click(await screen.findByText('notes.md'));
    fireEvent.click(screen.getByRole('button', { name: 'Mark Markdown Dirty' }));
    fireEvent.click(screen.getByRole('button', { name: '关闭 notes.md' }));

    expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument();
    expect(screen.queryByRole('tab', { name: 'notes.md' })).not.toBeInTheDocument();
  });

  it('owner handle 会发现并保存当前 epoch 的全部 dirty 文件', async () => {
    const owner = { ownerSessionId: 'test-session', ownerEpoch: 4 };
    const panelRef = createRef<ArtifactsPanelHandle>();
    vi.mocked(apiService.getSessionFiles).mockResolvedValueOnce({
      files: [{
        name: 'notes.md',
        path: 'notes.md',
        size: 64,
        type: 'md',
        modified: '2026-08-26T02:00:00Z',
        is_directory: false,
      }],
      total: 1,
    });
    render(
      <ArtifactsPanel
        ref={panelRef}
        sessionId={owner.ownerSessionId}
        ownerEpoch={owner.ownerEpoch}
        isOpen
        onClose={vi.fn()}
        variant="workspace"
      />,
    );
    fireEvent.click(await screen.findByText('notes.md'));
    fireEvent.click(screen.getByRole('button', { name: 'Mark Markdown Dirty' }));
    expect(panelRef.current?.hasDirty(owner)).toBe(true);

    let result: Awaited<ReturnType<ArtifactsPanelHandle['saveDirty']>> | undefined;
    await act(async () => {
      result = await panelRef.current?.saveDirty(owner);
    });
    expect(result).toMatchObject({ ...owner, ok: true, stale: false, failedPaths: [] });
    expect(panelRef.current?.hasDirty(owner)).toBe(false);
  });

  it('Markdown dirty 标签立即关闭，远端保存不再阻塞或弹放弃确认', async () => {
    vi.mocked(apiService.getSessionFiles).mockResolvedValueOnce({
      files: [{
        name: 'saving.md',
        path: 'saving.md',
        size: 64,
        type: 'md',
        modified: '2026-08-26T02:00:00Z',
        is_directory: false,
      }],
      total: 1,
    });
    render(
      <ArtifactsPanel
        sessionId="test-session"
        isOpen
        onClose={vi.fn()}
        variant="workspace"
      />,
    );

    fireEvent.click(await screen.findByText('saving.md'));
    fireEvent.click(screen.getByRole('button', { name: 'Mark Markdown Dirty' }));
    fireEvent.click(screen.getByRole('button', { name: '关闭 saving.md' }));

    expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument();
    expect(screen.queryByRole('tab', { name: 'saving.md' })).not.toBeInTheDocument();
  });

  it('工作台应该提供展开与收起面板按钮', () => {
    const onToggleExpanded = vi.fn();
    const { rerender } = render(
      <ArtifactsPanel
        sessionId="test-session"
        isOpen
        onClose={vi.fn()}
        variant="workspace"
        isExpanded={false}
        onToggleExpanded={onToggleExpanded}
      />,
    );

    fireEvent.click(screen.getByRole('button', { name: '展开面板' }));
    expect(onToggleExpanded).toHaveBeenCalledTimes(1);

    rerender(
      <ArtifactsPanel
        sessionId="test-session"
        isOpen
        onClose={vi.fn()}
        variant="workspace"
        isExpanded
        onToggleExpanded={onToggleExpanded}
      />,
    );
    expect(screen.getByRole('button', { name: '收起面板' })).toBeInTheDocument();
  });

  it('关闭预览后恢复文件列表滚动位置与触发焦点', async () => {
    render(
      <ArtifactsPanel
        sessionId="test-session"
        isOpen
        onClose={vi.fn()}
      />,
    );

    const fileLabel = await screen.findByText('report.pdf');
    const list = screen.getByTestId('artifacts-file-list');
    list.scrollTop = 180;
    const trigger = fileLabel.closest('[tabindex="0"]') as HTMLElement;
    fireEvent.click(trigger);
    fireEvent.click(await screen.findByRole('button', { name: 'Close Inline Preview' }));

    await waitFor(() => {
      expect(screen.getByTestId('artifacts-file-list').scrollTop).toBe(180);
      expect(document.activeElement).toHaveAttribute('data-file-path', '/workspace/report.pdf');
    });
  });

  it('切换 session 后旧根目录请求不得覆盖新会话文件', async () => {
    let resolveSessionA!: (value: { files: FileInfo[]; total: number }) => void;
    let resolveSessionB!: (value: { files: FileInfo[]; total: number }) => void;
    vi.mocked(apiService.getSessionFiles).mockImplementation((sessionId) => (
      new Promise((resolve) => {
        if (sessionId === 'session-a') resolveSessionA = resolve;
        else resolveSessionB = resolve;
      })
    ));

    const { rerender } = render(
      <ArtifactsPanel sessionId="session-a" isOpen onClose={vi.fn()} />,
    );
    await waitFor(() => expect(resolveSessionA).toBeTypeOf('function'));

    rerender(<ArtifactsPanel sessionId="session-b" isOpen onClose={vi.fn()} />);
    await waitFor(() => expect(resolveSessionB).toBeTypeOf('function'));

    await act(async () => {
      resolveSessionB({
        files: [{ ...mockFiles[0], name: 'new-session.pdf', path: 'new-session.pdf' }],
        total: 1,
      });
    });
    expect(await screen.findByText('new-session.pdf')).toBeInTheDocument();

    await act(async () => {
      resolveSessionA({
        files: [{ ...mockFiles[0], name: 'stale-session.pdf', path: 'stale-session.pdf' }],
        total: 1,
      });
    });
    expect(screen.queryByText('stale-session.pdf')).not.toBeInTheDocument();
    expect(screen.getByText('new-session.pdf')).toBeInTheDocument();
  });

  it('切换 session 时应该隔离并恢复各自的文件标签', async () => {
    vi.mocked(apiService.getSessionFiles).mockImplementation(async (sessionId) => ({
      files: sessionId === 'session-a'
        ? [{ ...mockFiles[0], name: 'session-a.pdf', path: 'session-a.pdf' }]
        : [{ ...mockFiles[0], name: 'session-b.pdf', path: 'session-b.pdf' }],
      total: 1,
    }));

    const { rerender } = render(
      <ArtifactsPanel sessionId="session-a" isOpen onClose={vi.fn()} variant="workspace" />,
    );
    fireEvent.click(await screen.findByText('session-a.pdf'));
    expect(await screen.findByText('Inline Preview: session-a.pdf')).toBeInTheDocument();

    rerender(
      <ArtifactsPanel sessionId="session-b" isOpen onClose={vi.fn()} variant="workspace" />,
    );
    expect(await screen.findByText('session-b.pdf')).toBeInTheDocument();
    expect(screen.queryByRole('tab', { name: 'session-a.pdf' })).not.toBeInTheDocument();

    rerender(
      <ArtifactsPanel sessionId="session-a" isOpen onClose={vi.fn()} variant="workspace" />,
    );
    expect(await screen.findByRole('tab', { name: 'session-a.pdf' })).toHaveAttribute('aria-selected', 'true');
    expect(screen.getByText('Inline Preview: session-a.pdf')).toBeInTheDocument();
  });

  it('A 的 pending close 不得在切回已打开同路径的 B 时关闭 B 标签', async () => {
    const sharedFile = {
      ...mockFiles[0],
      name: 'shared.md',
      path: 'shared.md',
      type: 'md',
    };
    vi.mocked(apiService.getSessionFiles).mockResolvedValue({ files: [sharedFile], total: 1 });

    const { rerender } = render(
      <ArtifactsPanel
        sessionId="session-b"
        ownerEpoch={1}
        isOpen
        onClose={vi.fn()}
        variant="workspace"
      />,
    );
    fireEvent.click(await screen.findByText('shared.md'));
    expect(await screen.findByRole('tab', { name: 'shared.md' })).toBeInTheDocument();

    rerender(
      <ArtifactsPanel
        sessionId="session-a"
        ownerEpoch={2}
        isOpen
        onClose={vi.fn()}
        variant="workspace"
      />,
    );
    fireEvent.click(await screen.findByText('shared.md'));
    fireEvent.click(screen.getByRole('button', { name: 'Mark Markdown Dirty' }));
    fireEvent.click(screen.getByRole('button', { name: '关闭 shared.md' }));

    rerender(
      <ArtifactsPanel
        sessionId="session-b"
        ownerEpoch={3}
        isOpen
        onClose={vi.fn()}
        variant="workspace"
      />,
    );
    expect(await screen.findByRole('tab', { name: 'shared.md' })).toBeInTheDocument();
    expect(screen.getByText('Inline Preview: shared.md')).toBeVisible();
  });

  it('从已有会话切到新建会话再返回时应该保留标签和活动预览', async () => {
    vi.mocked(apiService.getSessionFiles).mockResolvedValue({
      files: [{ ...mockFiles[0], name: 'session-a.pdf', path: 'session-a.pdf' }],
      total: 1,
    });

    const { rerender } = render(
      <ArtifactsPanel sessionId="session-a" isOpen onClose={vi.fn()} variant="workspace" />,
    );
    fireEvent.click(await screen.findByText('session-a.pdf'));
    expect(await screen.findByRole('tab', { name: 'session-a.pdf' })).toHaveAttribute('aria-selected', 'true');

    rerender(
      <ArtifactsPanel sessionId="" isOpen={false} onClose={vi.fn()} variant="workspace" />,
    );
    expect(screen.queryByRole('tab', { name: 'session-a.pdf' })).not.toBeInTheDocument();

    rerender(
      <ArtifactsPanel sessionId="session-a" isOpen onClose={vi.fn()} variant="workspace" />,
    );
    expect(await screen.findByRole('tab', { name: 'session-a.pdf' })).toHaveAttribute('aria-selected', 'true');
    expect(screen.getByText('Inline Preview: session-a.pdf')).toBeVisible();
  });

  it('应该为每个文件标签保留独立预览节点与滚动位置', async () => {
    const { container } = render(
      <ArtifactsPanel sessionId="test-session" isOpen onClose={vi.fn()} variant="workspace" />,
    );

    fireEvent.click(await screen.findByText('report.pdf'));
    const reportPane = container.querySelector<HTMLElement>('[data-preview-path="workspace/report.pdf"]')!;
    reportPane.scrollTop = 135;

    fireEvent.click(screen.getByRole('button', { name: '查看目录' }));
    fireEvent.click(await screen.findByText('data.xlsx'));
    const dataPane = container.querySelector<HTMLElement>('[data-preview-path="workspace/data.xlsx"]')!;
    dataPane.scrollTop = 42;

    fireEvent.click(screen.getByRole('tab', { name: 'report.pdf' }));
    expect(container.querySelector('[data-preview-path="workspace/report.pdf"]')).toBe(reportPane);
    expect(reportPane.scrollTop).toBe(135);
    expect(dataPane.scrollTop).toBe(42);
  });

  it('关闭活动标签后应该把焦点移到相邻活动标签', async () => {
    render(
      <ArtifactsPanel sessionId="test-session" isOpen onClose={vi.fn()} variant="workspace" />,
    );

    fireEvent.click(await screen.findByText('report.pdf'));
    fireEvent.click(screen.getByRole('button', { name: '查看目录' }));
    fireEvent.click(await screen.findByText('data.xlsx'));
    fireEvent.click(screen.getByRole('button', { name: '关闭 data.xlsx' }));

    await waitFor(() => {
      expect(screen.getByRole('tab', { name: 'report.pdf' })).toHaveFocus();
    });
  });

  it('应该允许显式刷新当前目录并在运行状态变化后自动刷新', async () => {
    const { rerender } = render(
      <ArtifactsPanel
        sessionId="test-session"
        isOpen
        onClose={vi.fn()}
        variant="workspace"
        refreshNonce="running"
      />,
    );
    await waitFor(() => expect(apiService.getSessionFiles).toHaveBeenCalledTimes(1));

    fireEvent.click(screen.getByRole('button', { name: '刷新当前目录' }));
    await waitFor(() => expect(apiService.getSessionFiles).toHaveBeenCalledTimes(2));

    rerender(
      <ArtifactsPanel
        sessionId="test-session"
        isOpen
        onClose={vi.fn()}
        variant="workspace"
        refreshNonce="completed"
      />,
    );
    await waitFor(() => expect(apiService.getSessionFiles).toHaveBeenCalledTimes(3));
  });

  it('文件和目录行支持 Enter/Space 键盘激活', async () => {
    render(
      <ArtifactsPanel
        sessionId="test-session"
        isOpen
        onClose={vi.fn()}
      />,
    );

    const fileTrigger = (await screen.findByText('report.pdf')).closest(
      '[role="button"]',
    ) as HTMLElement;
    fireEvent.keyDown(fileTrigger, { key: 'Enter' });
    expect(await screen.findByText('Inline Preview: report.pdf')).toBeInTheDocument();
  });

  it('可以按文件名筛选当前目录，并支持清空搜索', async () => {
    render(
      <ArtifactsPanel
        sessionId="test-session"
        isOpen
        onClose={vi.fn()}
      />,
    );

    const searchInput = await screen.findByRole('searchbox', { name: '搜索当前目录' });
    await screen.findByText('report.pdf');

    fireEvent.change(searchInput, { target: { value: 'REPORT' } });
    expect(screen.getByText('report.pdf')).toBeInTheDocument();
    expect(screen.queryByText('data.xlsx')).not.toBeInTheDocument();
    expect(screen.getByText('1 / 3 项')).toBeInTheDocument();

    fireEvent.change(searchInput, { target: { value: 'missing' } });
    expect(screen.getByText('未找到匹配项')).toBeInTheDocument();
    expect(screen.queryByText('空目录')).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: '清空搜索' }));
    expect(screen.getByText('data.xlsx')).toBeInTheDocument();
    expect(searchInput).toHaveValue('');
  });

  it('空文件列表应该显示空目录提示', async () => {
    vi.mocked(apiService.getSessionFiles).mockResolvedValue({
      files: [],
      total: 0,
    });

    render(
      <ArtifactsPanel
        sessionId="test-session"
        isOpen
        onClose={vi.fn()}
      />,
    );

    await waitFor(() => {
      expect(screen.getByText('空目录')).toBeInTheDocument();
    });
  });

  it('目录加载失败时应该显示可重试错误而不是伪装成空目录', async () => {
    const errorSpy = vi.spyOn(console, 'error').mockImplementation(() => {});
    vi.mocked(apiService.getSessionFiles)
      .mockRejectedValueOnce(new Error('network error'))
      .mockResolvedValueOnce({ files: mockFiles, total: mockFiles.length });

    render(
      <ArtifactsPanel sessionId="test-session" isOpen onClose={vi.fn()} />,
    );

    expect(await screen.findByText('无法读取此目录')).toBeInTheDocument();
    expect(screen.queryByText('空目录')).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '重试' }));
    expect(await screen.findByText('report.pdf')).toBeInTheDocument();
    errorSpy.mockRestore();
  });

  it('加载中应该显示加载动画', () => {
    vi.mocked(apiService.getSessionFiles).mockImplementation(
      () => new Promise(() => {}),
    );

    render(
      <ArtifactsPanel
        sessionId="test-session"
        isOpen
        onClose={vi.fn()}
      />,
    );

    expect(document.querySelector('.animate-spin')).toBeInTheDocument();
  });

  it('应该显示文件大小', async () => {
    render(
      <ArtifactsPanel
        sessionId="test-session"
        isOpen
        onClose={vi.fn()}
      />,
    );

    await waitFor(() => {
      expect(screen.getByText(/100.*KB/)).toBeInTheDocument();
    });
  });

  it('面板打开时应该有滑入动画类', () => {
    render(
      <ArtifactsPanel
        sessionId="test-session"
        isOpen
        onClose={vi.fn()}
      />,
    );

    const panel = screen.getByTestId('artifacts-panel-drawer');
    expect(panel.className).toContain('translate-x-0');
  });

  it('面板关闭时应该有滑出动画类', () => {
    const { rerender } = render(
      <ArtifactsPanel
        sessionId="test-session"
        isOpen
        onClose={vi.fn()}
      />,
    );

    rerender(
      <ArtifactsPanel
        sessionId="test-session"
        isOpen={false}
        onClose={vi.fn()}
      />,
    );

    const panel = screen.getByTestId('artifacts-panel-drawer');
    expect(panel.className).toContain('translate-x-full');
  });
});
