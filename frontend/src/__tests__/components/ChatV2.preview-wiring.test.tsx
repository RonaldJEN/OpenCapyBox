import { beforeEach, describe, expect, it, vi } from 'vitest';
import { act, fireEvent, render, screen, waitFor } from '../utils/test-utils';
import { ChatV2 } from '../../components/ChatV2';
import { apiService } from '../../services/api';
import { workspaceApi } from '../../services/workspaceApi';
import { emitWorkspaceMutation, resetWorkspaceEventsForTests } from '../../services/workspaceEvents';
import { makeChatV2DefaultProps } from '../utils/chatv2-helpers';

let lastChatInputProps: any = null;
let lastArtifactsPanelProps: any = null;
let lastFilePreviewProps: any = null;
let panelDirty = false;
const savePanelDrafts = vi.fn().mockResolvedValue({ ok: true, stale: false, failedPaths: [] });

vi.mock('../../services/api', () => ({
  apiService: {
    getSessionHistoryV2: vi.fn(),
    getSessionFiles: vi.fn(),
    sendMessageStreamV2: vi.fn(),
    uploadFile: vi.fn(),
    getRunningSessions: vi.fn(),
    createSession: vi.fn(),
    getUserId: vi.fn(() => 'demo-session'),
    getAuthHeaders: vi.fn(() => ({})),
  },
}));

vi.mock('../../services/chatStreamClient', () => ({
  startSendStream: vi.fn(() => ({ abort: vi.fn(), promise: Promise.resolve() })),
  startResumeStream: vi.fn(() => ({ abort: vi.fn(), promise: Promise.resolve() })),
  startSubscribeStream: vi.fn(() => ({
    abort: vi.fn(),
    promise: Promise.resolve(),
    getLatestSequence: () => 0,
  })),
}));

vi.mock('../../services/workspaceApi', () => ({
  workspaceApi: {
    getEntry: vi.fn(),
    versionContentUrl: vi.fn((versionId: string, preview = false) => (
      `/api/workspace/versions/${versionId}/content${preview ? '?preview=true' : ''}`
    )),
    contentPathUrl: vi.fn((path: string) => `/api/workspace/content?path=${encodeURIComponent(path)}&preview=true`),
  },
}));

vi.mock('../../components/ChatInput', () => ({
  ChatInput: (props: any) => {
    lastChatInputProps = props;
    return <div data-testid="chat-input-mock" />;
  },
}));

vi.mock('../../components/Round', () => ({
  Round: (props: any) => {
    const reference = props.round.assistant_file_references?.[0];
    const userAttachment = props.userAttachments?.[0];
    if (!reference) return (
      <div data-testid="round-without-file" data-user-attachments={props.userAttachments?.length || 0}>
        {userAttachment && (
          <button
            type="button"
            data-testid="open-user-attachment"
            onClick={() => props.onPreviewAttachment?.(userAttachment)}
          >
            user attachment
          </button>
        )}
      </div>
    );
    const file = reference.source === 'session'
      ? {
          source: 'session',
          session_id: reference.session_id,
          name: reference.name,
          path: reference.path,
          snapshot_path: reference.snapshot_path,
          size: reference.size,
          modified: reference.modified,
          type: reference.type,
          revision: reference.revision,
          content_mode: 'current',
          assistant_ref_id: reference.ref_id,
        }
      : {
          source: 'workspace',
          entry_id: reference.entry_id,
          workspace_path: reference.workspace_path,
          name: reference.name,
          path: reference.workspace_path,
          size: reference.size,
          modified: reference.modified,
          type: reference.type,
          revision: reference.revision,
          version_id: reference.version_id,
          content_mode: 'current',
          assistant_ref_id: reference.ref_id,
        };
    return (
      <div data-user-attachments={props.userAttachments?.length || 0}>
      <button type="button" data-testid="open-captured" onClick={() => props.onOpenFileInPanel?.(file)}>
        captured
      </button>
      </div>
    );
  },
}));

vi.mock('../../components/ArtifactsPanel', async () => {
  const { forwardRef, useImperativeHandle } = await import('react');
  return { ArtifactsPanel: forwardRef((props: any, ref) => {
    lastArtifactsPanelProps = props;
    useImperativeHandle(ref, () => ({ ownerSessionId: props.sessionId, ownerEpoch: props.ownerEpoch,
      hasDirty: () => panelDirty, pendingFileDrafts: () => [], saveDirty: savePanelDrafts,
    }));
    return <div data-testid="artifacts-panel" data-open={String(props.isOpen)} />;
  }) };
});

vi.mock('../../components/FilePreview', () => ({
  FilePreview: (props: any) => {
    lastFilePreviewProps = props;
    return <div data-testid="file-preview" />;
  },
}));

describe('ChatV2 structured assistant file wiring', () => {
  const defaultProps = makeChatV2DefaultProps();

  it.each(['user', 'assistant', 'assistant-fallback'])('关闭后拒绝迟到的 %s 打开意图', async (kind) => {
    let resolve!: (value: any) => void;
    let reject!: (reason: Error) => void;
    const pending = new Promise<any>((yes, no) => { resolve = yes; reject = no; });
    vi.mocked(apiService.getSessionFiles).mockReturnValue(pending);
    const file = { source: 'session' as const, session_id: 'test-session', path: 'report.pdf', name: 'report.pdf', size: 0, type: 'pdf' };
    vi.mocked(apiService.getSessionHistoryV2).mockResolvedValue({
      session_id: 'test-session', total: 1, rounds: [{
        round_id: 'round-late', user_message: 'test', final_response: 'done',
        user_attachments: kind === 'user' ? [file] : [],
        assistant_file_references: kind !== 'user' ? [{ ...file, ref_id: 'ref-late', revision: '1', modified: '', snapshot_path: 'snapshot.pdf' }] : [],
        steps: [], step_count: 0, status: 'completed', created_at: '2026-09-10T08:00:00Z',
      }],
    });
    render(<ChatV2 sessionId="test-session" {...defaultProps} />);
    fireEvent.click(await screen.findByTestId(kind === 'user' ? 'open-user-attachment' : 'open-captured'));
    await waitFor(() => expect(apiService.getSessionFiles).toHaveBeenCalled());
    panelDirty = true;
    act(() => lastArtifactsPanelProps.onClose());
    expect(savePanelDrafts).toHaveBeenCalledWith(expect.objectContaining({ ownerSessionId: 'test-session' }));
    await act(async () => {
      if (kind === 'assistant-fallback') reject(new Error('unavailable'));
      else resolve({ files: [{ ...file, size: 100 }], total: 1 });
      await pending.catch(() => undefined);
    });
    expect(lastArtifactsPanelProps.isOpen).toBe(false);
    expect(lastArtifactsPanelProps.targetFile).toBeFalsy();
  });

  beforeEach(() => {
    vi.clearAllMocks();
    panelDirty = false;
    resetWorkspaceEventsForTests();
    lastChatInputProps = null;
    lastArtifactsPanelProps = null;
    lastFilePreviewProps = null;
    vi.mocked(apiService.getSessionHistoryV2).mockResolvedValue({
      rounds: [],
      session_id: 'test-session',
      total: 0,
    });
    vi.mocked(apiService.getSessionFiles).mockResolvedValue({ files: [], total: 0 });
    vi.mocked(workspaceApi.getEntry).mockResolvedValue({
      entry_id: 'entry-1', parent_id: null, name: 'daily.md', kind: 'file',
      path: 'reports/daily.md', size_bytes: 80, mime_type: 'text/markdown',
      sha256: 'a'.repeat(64),
      revision: 4, current_version_id: 'version-4', tree_revision: 1,
      status: 'active', created_at: '2026-08-28T10:00:00Z', updated_at: '2026-08-28T10:10:00Z',
    });
  });

  it('optimistic Workspace 附件没有 snapshot 时不进入 Session 预览', async () => {
    vi.mocked(apiService.getSessionHistoryV2).mockResolvedValue({
      session_id: 'test-session', total: 1, rounds: [{
        round_id: 'temp-round', user_message: '读取工作区附件', final_response: '',
        user_attachments: [{
          source: 'workspace', entry_id: 'entry-1', name: 'rates.xlsx',
          path: 'reports/rates.xlsx', origin_path: 'reports/rates.xlsx',
          revision: 4, size: 18_367, type: 'xlsx',
        }],
        steps: [], step_count: 0, status: 'running', created_at: '2026-09-02T09:20:00Z',
      }],
    });
    render(<ChatV2 sessionId="test-session" {...defaultProps} />);

    fireEvent.click(await screen.findByTestId('open-user-attachment'));

    expect(await screen.findByText('工作区附件正在准备，请稍后再打开。')).toBeInTheDocument();
    expect(lastArtifactsPanelProps).toMatchObject({ isOpen: false });
    expect(lastArtifactsPanelProps.targetFile).toBeFalsy();
    expect(apiService.getSessionFiles).not.toHaveBeenCalled();
  });

  it.each([[0, 1], [1, 0], [0, 1, 0, 1]])('切换附件顺序 %j 后关闭保留最新阅读位置', async (...order) => {
    vi.mocked(apiService.getSessionHistoryV2).mockResolvedValue({
      session_id: 'test-session', total: 2,
      rounds: ['v39.zip', 'v54.zip'].map((name, index) => ({
        round_id: `round-${index}`, user_message: name, final_response: 'done',
        user_attachments: [{ source: 'session', session_id: 'test-session', name, path: name, size: 100, type: 'zip' }],
        steps: [], step_count: 0, status: 'completed', created_at: '2026-09-07T09:00:00Z',
      })),
    });
    const { container } = render(<ChatV2 sessionId="test-session" {...defaultProps} />);
    await waitFor(() => expect(screen.getAllByTestId('open-user-attachment')).toHaveLength(2));
    const chatArea = container.querySelector('div[class*="overflow-y-auto"][class*="bg-claude-bg"]') as HTMLDivElement;
    Object.defineProperties(chatArea, {
      scrollTop: { configurable: true, writable: true, value: 200 },
      scrollHeight: { configurable: true, value: 5000 },
      clientWidth: { configurable: true, value: 600 },
      clientHeight: { configurable: true, value: 600 },
    });
    for (const index of order) {
      chatArea.scrollTop = index === 0 ? 200 : 3200;
      fireEvent.scroll(chatArea);
      fireEvent.click(screen.getAllByTestId('open-user-attachment')[index]);
      await waitFor(() => expect(lastArtifactsPanelProps.targetFile?.name).toBe(index === 0 ? 'v39.zip' : 'v54.zip'));
    }
    // Reading may continue after the latest attachment was opened.
    chatArea.scrollTop = 1800;
    fireEvent.scroll(chatArea);
    act(() => lastArtifactsPanelProps.onClose());
    expect(lastArtifactsPanelProps.isOpen).toBe(false);
    expect(chatArea.scrollTop).toBe(1800);
  });

  it.each(['new-assistant-file', 'new-session'])('迟到用户附件不能覆盖 %s', async (next) => {
    let finish!: (value: any) => void;
    vi.mocked(apiService.getSessionFiles).mockReturnValueOnce(new Promise((resolve) => { finish = resolve; }));
    const oldFile = { name: 'old.pdf', path: 'old.pdf', size: 0, type: 'pdf', modified: '', source: 'session' as const, session_id: 'test-session' };
    const newFile = { ...oldFile, name: 'new.pdf', path: 'new.pdf', size: 20 };
    const base = { user_message: 'files', final_response: 'done', steps: [], step_count: 0, status: 'completed', created_at: '2026-09-10T08:00:00Z' };
    vi.mocked(apiService.getSessionHistoryV2).mockResolvedValue({ session_id: 'test-session', total: 2, rounds: [
      { ...base, round_id: 'old-round', user_attachments: [oldFile] },
      { ...base, round_id: 'new-round', assistant_file_references: [{ ...newFile, ref_id: 'new-ref', revision: '1', modified: '' }] },
    ] });
    const view = render(<ChatV2 sessionId="test-session" {...defaultProps} />);
    fireEvent.click(await screen.findByTestId('open-user-attachment'));
    await waitFor(() => expect(apiService.getSessionFiles).toHaveBeenCalledTimes(1));
    if (next === 'new-session') {
      vi.mocked(apiService.getSessionHistoryV2).mockResolvedValue({ session_id: 'other', rounds: [], total: 0 });
      view.rerender(<ChatV2 sessionId="other" {...defaultProps} />);
      await waitFor(() => expect(lastArtifactsPanelProps.sessionId).toBe('other'));
    } else {
      vi.mocked(apiService.getSessionFiles).mockResolvedValue({ files: [newFile], total: 1 });
      fireEvent.click(screen.getByTestId('open-captured'));
      await waitFor(() => expect(lastArtifactsPanelProps.targetFile?.name).toBe('new.pdf'));
    }
    await act(async () => { finish({ files: [{ ...oldFile, size: 10 }], total: 1 }); });
    if (next === 'new-session') expect(lastArtifactsPanelProps.isOpen).toBe(false);
    else expect(lastArtifactsPanelProps.targetFile?.name).toBe('new.pdf');
  });

  it('authoritative Workspace snapshot 仍以只读 Session 快照打开', async () => {
    const snapshotPath = '.workspace-snapshots/entry-1/capture-1/rates.xlsx';
    vi.mocked(apiService.getSessionHistoryV2).mockResolvedValue({
      session_id: 'test-session', total: 1, rounds: [{
        round_id: 'round-1', user_message: '读取工作区附件', final_response: 'done',
        user_attachments: [{
          source: 'workspace', entry_id: 'entry-1', name: 'rates.xlsx',
          path: snapshotPath, snapshot_path: snapshotPath,
          origin_path: 'reports/rates.xlsx', version_id: 'version-1',
          revision: '4', size: 18_367, type: 'xlsx',
        }],
        steps: [], step_count: 0, status: 'completed', created_at: '2026-09-02T09:20:00Z',
      }],
    });
    render(<ChatV2 sessionId="test-session" {...defaultProps} />);

    fireEvent.click(await screen.findByTestId('open-user-attachment'));

    await waitFor(() => expect(lastArtifactsPanelProps).toMatchObject({
      isOpen: true,
      targetFile: expect.objectContaining({
        path: snapshotPath,
        snapshot_path: snapshotPath,
        content_mode: 'captured',
      }),
    }));
  });

  it('Workspace 永久删除后立即移除当前 Chat 投影中的附件和助手文件引用', async () => {
    vi.mocked(apiService.getSessionHistoryV2).mockResolvedValue({
      session_id: 'test-session',
      total: 1,
      rounds: [{
        round_id: 'round-workspace-delete',
        user_message: '请读取附件',
        user_attachments: [{
          source: 'workspace', entry_id: 'entry-1', name: 'daily.md',
          path: '.workspace-snapshots/entry-1/version-1/daily.md',
          origin_path: 'reports/daily.md', revision: '4', version_id: 'version-4',
          size: 80, type: 'text/markdown',
        }],
        assistant_file_references: [{
          ref_id: 'workspace:entry-1:version-4', source: 'workspace',
          entry_id: 'entry-1', version_id: 'version-4', workspace_path: 'reports/daily.md',
          name: 'daily.md', path: 'reports/daily.md', size: 80, modified: '',
          type: 'md', revision: '4',
        }],
        preferred_skills: [], preferred_mcp_connections: [], final_response: 'done',
        steps: [], step_count: 0, status: 'completed', created_at: '2026-09-01T00:00:00Z',
      }],
    });
    render(<ChatV2 sessionId="test-session" {...defaultProps} />);
    expect(await screen.findByTestId('open-captured')).toBeInTheDocument();
    expect(screen.getByTestId('open-captured').parentElement).toHaveAttribute('data-user-attachments', '1');

    act(() => {
      emitWorkspaceMutation({
        operation: 'delete',
        tombstone: true,
        affectedEntryIds: ['entry-1'],
        origin: 'local',
      });
    });

    await waitFor(() => expect(screen.queryByTestId('open-captured')).not.toBeInTheDocument());
    expect(screen.getByTestId('round-without-file')).toHaveAttribute('data-user-attachments', '0');
  });

  it('没有 session 时仍按 entry_id 打开工作区附件', async () => {
    render(<ChatV2 sessionId="" {...defaultProps} />);
    await waitFor(() => expect(lastChatInputProps).toBeTruthy());
    const navigation = vi.fn();
    window.addEventListener('workspace:navigate', navigation);
    await act(async () => {
      await lastChatInputProps.onPreviewAttachment({
        source: 'workspace', entry_id: 'entry-1', workspace_path: 'workspace.md',
        path: 'workspace.md', name: 'workspace.md', revision: 2,
        size: 10, modified: 'now', type: 'md',
      });
    });
    expect((navigation.mock.calls[0][0] as CustomEvent).detail).toEqual({
      entryId: 'entry-1',
    });
    window.removeEventListener('workspace:navigate', navigation);
  });

  it('Session 卡重新核验并打开当前文件', async () => {
    vi.mocked(apiService.getSessionHistoryV2).mockResolvedValue({
      session_id: 'test-session', total: 1, rounds: [{
        round_id: 'round-1', user_message: '生成报告', final_response: '已生成。',
        steps: [], step_count: 1, status: 'completed', created_at: '2026-08-28T10:00:00Z',
        assistant_file_references: [{
          ref_id: 'session:test-session:round-1:report', source: 'session',
          session_id: 'test-session', name: 'report.md', path: 'report.md',
          snapshot_path: '.assistant-artifacts/round-1/report/report.md',
          size: 42, modified: '2026-08-28T10:00:00Z', type: 'md', revision: 'v1:42:100',
        }],
      }],
    });
    vi.mocked(apiService.getSessionFiles).mockResolvedValue({
      total: 1,
      files: [{
        name: 'report.md', path: 'report.md', size: 55,
        modified: '2026-08-28T11:00:00Z', type: 'md', revision: 'v1:55:200',
        is_directory: false,
      }],
    });
    render(<ChatV2 sessionId="test-session" {...defaultProps} />);
    fireEvent.click(await screen.findByTestId('open-captured'));
    await waitFor(() => expect(lastArtifactsPanelProps.targetFile).toMatchObject({
      path: 'report.md', size: 55, revision: 'v1:55:200', content_mode: 'current',
    }));
    expect(apiService.getSessionFiles).toHaveBeenCalledWith('test-session', undefined);
    expect(lastFilePreviewProps).toBeNull();
  });

  it('Session 当前文件删除后只读展示生成时快照', async () => {
    vi.mocked(apiService.getSessionHistoryV2).mockResolvedValue({
      session_id: 'test-session', total: 1, rounds: [{
        round_id: 'round-1', user_message: '生成报告', final_response: '已生成。',
        steps: [], step_count: 1, status: 'completed', created_at: '2026-08-28T10:00:00Z',
        assistant_file_references: [{
          ref_id: 'session:test-session:round-1:report', source: 'session',
          session_id: 'test-session', name: 'report.md', path: 'report.md',
          snapshot_path: '.assistant-artifacts/round-1/report/report.md',
          size: 42, modified: '2026-08-28T10:00:00Z', type: 'md', revision: 'v1:42:100',
        }],
      }],
    });
    render(<ChatV2 sessionId="test-session" {...defaultProps} />);
    fireEvent.click(await screen.findByTestId('open-captured'));
    await waitFor(() => expect(lastArtifactsPanelProps).toMatchObject({
      isOpen: true,
      targetContextNotice: '当前会话文件已删除，正在显示生成时版本。',
      targetFile: expect.objectContaining({
        path: '.assistant-artifacts/round-1/report/report.md', content_mode: 'captured',
      }),
    }));
  });

  it('Workspace 卡按稳定 entry_id 打开当前文件', async () => {
    vi.mocked(apiService.getSessionHistoryV2).mockResolvedValue({
      session_id: 'test-session', total: 1, rounds: [{
        round_id: 'round-1', user_message: '更新报告', final_response: '已更新。',
        steps: [], step_count: 1, status: 'completed', created_at: '2026-08-28T10:00:00Z',
        assistant_file_references: [{
          ref_id: 'workspace:entry-1:version-3', source: 'workspace', entry_id: 'entry-1',
          version_id: 'version-3', workspace_path: 'reports/daily.md', name: 'daily.md',
          path: 'reports/daily.md', size: 80, modified: '', type: 'md', revision: '3',
        }],
      }],
    });
    const navigation = vi.fn();
    window.addEventListener('workspace:navigate', navigation);
    render(<ChatV2 sessionId="test-session" {...defaultProps} />);
    fireEvent.click(await screen.findByTestId('open-captured'));
    await waitFor(() => expect(navigation).toHaveBeenCalledTimes(1));
    expect((navigation.mock.calls[0][0] as CustomEvent).detail).toEqual({
      entryId: 'entry-1',
    });
    expect(lastFilePreviewProps).toBeNull();
    window.removeEventListener('workspace:navigate', navigation);
  });

  it('Workspace 文件删除后不回退历史版本也不重建', async () => {
    vi.mocked(workspaceApi.getEntry).mockRejectedValue({ status: 404 });
    vi.mocked(apiService.getSessionHistoryV2).mockResolvedValue({
      session_id: 'test-session', total: 1, rounds: [{
        round_id: 'round-1', user_message: '更新报告', final_response: '已更新。',
        steps: [], step_count: 1, status: 'completed', created_at: '2026-08-28T10:00:00Z',
        assistant_file_references: [{
          ref_id: 'workspace:entry-1:version-3', source: 'workspace', entry_id: 'entry-1',
          version_id: 'version-3', workspace_path: 'reports/daily.md', name: 'daily.md',
          path: 'reports/daily.md', size: 80, modified: '', type: 'md', revision: '3',
        }],
      }],
    });
    render(<ChatV2 sessionId="test-session" {...defaultProps} />);
    fireEvent.click(await screen.findByTestId('open-captured'));
    expect(await screen.findByText('工作区文件已删除或暂时无法读取；已删除的文件不保留历史副本。')).toBeInTheDocument();
    expect(lastFilePreviewProps).toBeNull();
  });
});
