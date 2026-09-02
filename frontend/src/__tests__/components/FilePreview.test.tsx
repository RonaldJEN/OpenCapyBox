import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { createRef, forwardRef, useEffect, useImperativeHandle, useRef, useState } from 'react';
import { describe, expect, it, vi, beforeEach, afterEach, afterAll } from 'vitest';
import JSZip from 'jszip';
import mammoth from 'mammoth';
import type { FileInfo } from '../../types';
import {
  flushSessionDraft,
  queueSessionMarkdownDraft,
  resetSessionDraftOutboxForTests,
} from '../../services/sessionDraftOutbox';
import { resetWorkspaceDraftOutboxForTests } from '../../services/workspaceDraftOutbox';
import {
  FilePreview,
  type FilePreviewHandle,
  HTML_INLINE_PREVIEW_TIMEOUT_MS,
  MARKDOWN_AUTOSAVE_DELAY_MS,
  OFFICE_PREVIEW_SLOW_HINT_MS,
  SPREADSHEET_AUTOSAVE_DELAY_MS,
  parseCsvRows,
  scheduleHtmlInlinePreviewTimeout,
} from '../../components/FilePreview';

const originalCreateObjectURL = Object.getOwnPropertyDescriptor(URL, 'createObjectURL');
const originalRevokeObjectURL = Object.getOwnPropertyDescriptor(URL, 'revokeObjectURL');
let markdownAdapterInitializationCount = 0;

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, reject, resolve };
}

function mockObjectUrlApis(...urls: string[]) {
  const createObjectURLMock = vi.fn();
  urls.forEach((url) => {
    createObjectURLMock.mockReturnValueOnce(url);
  });
  createObjectURLMock.mockReturnValue(urls[urls.length - 1] || 'blob:default');

  const revokeObjectURLMock = vi.fn();

  Object.defineProperty(URL, 'createObjectURL', {
    configurable: true,
    writable: true,
    value: createObjectURLMock,
  });
  Object.defineProperty(URL, 'revokeObjectURL', {
    configurable: true,
    writable: true,
    value: revokeObjectURLMock,
  });

  return { createObjectURLMock, revokeObjectURLMock };
}

function restoreObjectUrlApis() {
  if (originalCreateObjectURL) {
    Object.defineProperty(URL, 'createObjectURL', originalCreateObjectURL);
  } else {
    delete (URL as { createObjectURL?: unknown }).createObjectURL;
  }

  if (originalRevokeObjectURL) {
    Object.defineProperty(URL, 'revokeObjectURL', originalRevokeObjectURL);
  } else {
    delete (URL as { revokeObjectURL?: unknown }).revokeObjectURL;
  }
}

const { getAuthHeadersMock, updateSessionMarkdownMock, updateSessionSpreadsheetMock, getSessionFilesMock, workspaceListGetMock, workspacePostMock } = vi.hoisted(() => ({
  getAuthHeadersMock: vi.fn(() => ({ Authorization: 'Bearer test-token' })),
  updateSessionMarkdownMock: vi.fn(),
  updateSessionSpreadsheetMock: vi.fn(),
  getSessionFilesMock: vi.fn(),
  workspaceListGetMock: vi.fn(),
  workspacePostMock: vi.fn(),
}));

vi.mock('../../services/api', () => ({
  apiService: {
    getAuthHeaders: getAuthHeadersMock,
    getAxiosClient: () => ({ get: workspaceListGetMock, post: workspacePostMock }),
    downloadFile: vi.fn(),
    updateSessionMarkdown: updateSessionMarkdownMock,
    updateSessionSpreadsheet: updateSessionSpreadsheetMock,
    getSessionFiles: getSessionFilesMock,
  },
}));

vi.mock('mammoth', () => ({
  default: {
    convertToHtml: vi.fn(),
  },
}));

vi.mock('react-syntax-highlighter', () => ({
  Prism: ({ children }: { children: string }) => <pre>{children}</pre>,
}));

vi.mock('react-syntax-highlighter/dist/esm/styles/prism', () => ({
  vscDarkPlus: {},
}));

vi.mock('../../components/file-preview/SlideDeckPreview', () => ({
  SlideDeckPreview: ({
    sourceKey,
    requestId,
    activeRequestId,
    title,
    onReady,
  }: {
    sourceKey: string;
    requestId: number;
    activeRequestId: number;
    title: string;
    onReady?: (sourceKey: string) => void;
  }) => {
    useEffect(() => {
      if (requestId === activeRequestId) onReady?.(sourceKey);
    }, [activeRequestId, onReady, requestId, sourceKey]);
    return <div data-testid="slide-deck-preview" data-source-key={sourceKey}>{title}</div>;
  },
}));

vi.mock('../../components/file-preview/VditorMarkdownEditor', () => {
  const MockMarkdownEditor = forwardRef(function MockMarkdownEditor({
    markdown,
    onChange,
  }: {
    markdown: string;
    onChange: (markdown: string) => void;
  }, ref) {
    const valueRef = useRef(markdown);
    useEffect(() => { valueRef.current = markdown; }, [markdown]);
    useEffect(() => {
      markdownAdapterInitializationCount += 1;
    }, []);
    useImperativeHandle(ref, () => ({ getMarkdown: () => valueRef.current }), []);
    return (
      <textarea
        aria-label="Markdown 所见即所得编辑器"
        value={markdown}
        onChange={(event) => {
          valueRef.current = event.target.value;
          onChange(event.target.value);
        }}
      />
    );
  });
  return {
    VditorMarkdownEditor: MockMarkdownEditor,
  };
});

vi.mock('../../components/file-preview/SpreadsheetEditor', () => ({
  SpreadsheetEditor: forwardRef(function MockSpreadsheetEditor({
    source,
    fileName,
    readOnly,
    onMutation,
  }: {
    source: ArrayBuffer;
    fileName: string;
    readOnly?: boolean;
    onMutation?: () => void;
  }, ref) {
    useImperativeHandle(ref, () => ({
      exportFile: () => new Uint8Array([0x50, 0x4b, 0x03, 0x04]).buffer,
    }), []);
    return (
      <div data-testid="spreadsheet-editor" data-readonly={String(Boolean(readOnly))} data-source-size={source.byteLength}>
        <span>{fileName}</span>
        {!readOnly && <button type="button" onClick={onMutation}>模拟编辑单元格</button>}
      </div>
    );
  }),
}));

async function findMarkdownEditor() {
  return screen.findByRole('textbox', { name: 'Markdown 所见即所得编辑器' });
}

describe('FilePreview custom source', () => {
  const markdownFile: FileInfo = {
    name: 'report.md',
    path: 'report.md',
    size: 100,
    modified: '2026-04-16T18:30:00Z',
    type: 'md',
    is_directory: false,
  };

  beforeEach(() => {
    resetSessionDraftOutboxForTests();
    resetWorkspaceDraftOutboxForTests();
    vi.clearAllMocks();
    markdownAdapterInitializationCount = 0;
    workspaceListGetMock.mockResolvedValue({
      data: { items: [], next_cursor: null, workspace_revision: 1 },
    });
    workspacePostMock.mockResolvedValue({
      data: {
        status: 'CREATED',
        mutation_id: 'mutation-import',
        entry: {
          entry_id: 'workspace-file', parent_id: null, name: 'report.md', kind: 'file', path: 'report.md',
          size_bytes: 120, mime_type: 'text/markdown', sha256: 'hash', revision: 1, status: 'active',
          created_at: '2026-04-16T18:31:00Z', updated_at: '2026-04-16T18:31:00Z',
        },
      },
    });
    getSessionFilesMock.mockResolvedValue({ files: [], total: 0 });
    mockObjectUrlApis('blob:default');
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  afterAll(() => {
    restoreObjectUrlApis();
  });

  it('uses custom preview URL and custom download handler', async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      text: () => Promise.resolve('# hello preview'),
    });
    vi.stubGlobal('fetch', fetchMock);

    const onDownloadFile = vi.fn().mockResolvedValue(undefined);

    render(
      <FilePreview
        file={markdownFile}
        sessionId="ignored-session-id"
        onClose={() => {}}
        previewUrlBuilder={(f) => `/api/cron/runs/run-1/files/${encodeURIComponent(f.path)}?preview=true`}
        onDownloadFile={onDownloadFile}
      />,
    );

    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith('/api/cron/runs/run-1/files/report.md?preview=true', expect.objectContaining({
        headers: { Authorization: 'Bearer test-token' },
        signal: expect.any(AbortSignal),
      }));
    });

    expect(await screen.findByText('hello preview')).toBeInTheDocument();

    fireEvent.click(screen.getByTitle('下载文件'));
    await waitFor(() => {
      expect(onDownloadFile).toHaveBeenCalledWith(markdownFile);
    });
  });

  it('父级流式重渲染时不会因等价的预览 URL 回调重新加载 XLSX', async () => {
    const workspaceFile: FileInfo = {
      name: 'workspace.xlsx',
      path: 'workspace.xlsx',
      size: 1988,
      modified: '2026-08-27T11:34:00Z',
      revision: 1,
      type: 'xlsx',
    };
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      arrayBuffer: () => Promise.resolve(new ArrayBuffer(8)),
    });
    vi.stubGlobal('fetch', fetchMock);

    const renderPreview = () => (
      <FilePreview
        file={workspaceFile}
        sessionId="workspace:persistent"
        onClose={() => {}}
        previewUrlBuilder={(file) => `/api/workspace/entries/entry-1/content?preview=true&path=${file.path}`}
      />
    );
    const { rerender } = render(renderPreview());

    const editor = await screen.findByTestId('spreadsheet-editor');
    expect(fetchMock).toHaveBeenCalledTimes(1);

    rerender(renderPreview());

    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(screen.getByTestId('spreadsheet-editor')).toBe(editor);
  });

  it('空白 XLSX 首次加载期间重命名会改用新路径继续加载', async () => {
    const fetchMock = vi.fn()
      .mockImplementationOnce(() => new Promise(() => {}))
      .mockResolvedValueOnce({
        ok: true,
        arrayBuffer: () => Promise.resolve(new ArrayBuffer(8)),
      });
    vi.stubGlobal('fetch', fetchMock);
    const renderPreview = (file: FileInfo) => (
      <FilePreview
        inline
        refreshInPlace
        file={file}
        sessionId="workspace:persistent"
        onClose={() => {}}
        onSaveSpreadsheetFile={vi.fn()}
      />
    );
    const original: FileInfo = {
      name: '未命名.xlsx',
      path: '未命名.xlsx',
      source: 'workspace',
      entry_id: 'entry-xlsx-rename',
      version_id: 'version-1',
      size: 1537,
      modified: '2026-08-28T10:00:00Z',
      revision: 1,
      type: 'xlsx',
    };
    const { rerender } = render(renderPreview(original));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));

    rerender(renderPreview({
      ...original,
      name: 'renamed.xlsx',
      path: 'renamed.xlsx',
      workspace_path: 'renamed.xlsx',
      revision: 2,
      modified: '2026-08-28T10:00:01Z',
    }));

    expect(await screen.findByTestId('spreadsheet-editor')).toHaveTextContent('renamed.xlsx');
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(screen.queryByText('电子表格内容为空')).not.toBeInTheDocument();
  });

  it('同一工作区文件外部更新时保留旧内容并在右侧预览原位换成新版本', async () => {
    let resolveRefresh!: (response: { ok: boolean; text: () => Promise<string> }) => void;
    const fetchMock = vi.fn()
      .mockResolvedValueOnce({
        ok: true,
        text: () => Promise.resolve('# 旧版本'),
      })
      .mockImplementationOnce(() => new Promise((resolve) => { resolveRefresh = resolve; }));
    vi.stubGlobal('fetch', fetchMock);
    const renderPreview = (file: FileInfo) => (
      <FilePreview
        inline
        refreshInPlace
        file={file}
        sessionId="workspace:persistent"
        onClose={() => {}}
        previewUrlBuilder={() => '/api/workspace/entries/entry-1/content?preview=true'}
      />
    );
    const original = {
      ...markdownFile,
      source: 'workspace' as const,
      entry_id: 'entry-1',
      workspace_path: markdownFile.path,
      version_id: 'version-1',
      revision: 1,
    };
    const { rerender } = render(renderPreview(original));
    const editor = await findMarkdownEditor();
    expect(editor).toHaveValue('# 旧版本');
    const preview = screen.getByTestId('file-preview-inline');

    rerender(renderPreview({
      ...original,
      version_id: 'version-2',
      revision: 2,
    }));

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
    expect(editor).toHaveValue('# 旧版本');
    expect(screen.queryByTestId('file-preview-loading')).not.toBeInTheDocument();
    expect(screen.getByRole('status')).toHaveTextContent('正在更新文件');
    expect(screen.getByTestId('file-preview-inline')).toBe(preview);

    await act(async () => {
      resolveRefresh({ ok: true, text: () => Promise.resolve('# 新版本') });
    });
    await waitFor(() => expect(editor).toHaveValue('# 新版本'));
    expect(screen.queryByText('正在更新文件')).not.toBeInTheDocument();
    expect(screen.getByTestId('file-preview-inline')).toBe(preview);
  });

  it.each([false, true])('合并正文与基线一起接纳，读取期间续写不被覆盖（continued=%s）', async (continued) => {
    const owner = { ownerSessionId: 'workspace:persistent', ownerEpoch: 0 };
    const current: FileInfo = {
      ...markdownFile, source: 'workspace', entry_id: `merged-${continued}`,
      path: `merged-${continued}.md`, name: `merged-${continued}.md`,
      revision: 1, version_id: 'base-version',
    };
    const firstRead = deferred<{ ok: boolean; text: () => Promise<string> }>();
    const finalText = `Human: ${continued ? 'edit2' : 'edit1'}\n\nAI: new\n`;
    const fetchMock = vi.fn()
      .mockResolvedValueOnce({ ok: true, text: async () => 'Human: old\n\nAI: old\n' })
      .mockImplementationOnce(() => firstRead.promise)
      .mockResolvedValue({ ok: true, text: async () => finalText });
    vi.stubGlobal('fetch', fetchMock);
    const { workspaceApi } = await import('../../services/workspaceApi');
    let saves = 0;
    const update = vi.spyOn(workspaceApi, 'updateContent').mockImplementation(async () => ({
      status: 'UPDATED', mutation_id: `mutation-${++saves}`, auto_merged: true,
      entry: {
        entry_id: current.entry_id!, parent_id: null, name: current.name, kind: 'file',
        path: current.path, size_bytes: finalText.length, mime_type: 'text/markdown', sha256: 'sha',
        revision: saves + 2, current_version_id: `merged-version-${saves}`, status: 'active',
        created_at: current.modified, updated_at: `2026-08-30T00:00:0${saves}Z`,
      },
    }));
    const ref = createRef<FilePreviewHandle>();
    const updated = vi.fn();
    render(<FilePreview ref={ref} inline refreshInPlace file={current} sessionId={owner.ownerSessionId}
      onFileUpdated={updated} onClose={() => {}} />);
    const editor = await findMarkdownEditor();
    const initialEditorMounts = markdownAdapterInitializationCount;
    fireEvent.change(editor, { target: { value: 'Human: edit1\n\nAI: old\n' } });
    let saving!: Promise<unknown>;
    act(() => { saving = ref.current!.saveDirty(owner); });
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith(
      '/api/workspace/versions/merged-version-1/content?preview=true', expect.any(Object),
    ));
    expect(updated).not.toHaveBeenCalled();
    if (continued) fireEvent.change(editor, { target: { value: 'Human: edit2\n\nAI: old\n' } });
    await act(async () => {
      firstRead.resolve({ ok: true, text: async () => 'Human: edit1\n\nAI: new\n' });
      await saving;
    });
    expect(editor).toHaveValue(finalText);
    expect(ref.current!.isDirty(owner)).toBe(false);
    expect(update).toHaveBeenCalledTimes(continued ? 2 : 1);
    if (continued) {
      expect(update.mock.calls[1][0]).toMatchObject({ revision: 1, current_version_id: 'base-version' });
      expect(update.mock.calls[1][1]).toBe('Human: edit2\n\nAI: old\n');
    }
    expect(updated).toHaveBeenCalledTimes(1);
    expect(updated.mock.calls[0][0].version_id).toBe(`merged-version-${continued ? 2 : 1}`);
    expect(markdownAdapterInitializationCount).toBe(initialEditorMounts);
  });

  it('Workspace 终态保存失败丢弃草稿、恢复服务端正文并显示文件内提示', async () => {
    const owner = { ownerSessionId: 'workspace:persistent', ownerEpoch: 0 };
    const current: FileInfo = {
      ...markdownFile,
      source: 'workspace',
      entry_id: 'terminal-failure-entry',
      path: 'terminal-failure.md',
      name: 'terminal-failure.md',
      revision: 1,
      version_id: 'server-version-1',
    };
    const fetchMock = vi.fn()
      .mockResolvedValueOnce({ ok: true, text: async () => '# 服务端旧正文' })
      .mockResolvedValueOnce({ ok: true, text: async () => '# 服务端恢复正文' });
    vi.stubGlobal('fetch', fetchMock);
    const { WorkspaceApiError, workspaceApi } = await import('../../services/workspaceApi');
    vi.spyOn(workspaceApi, 'updateContent').mockRejectedValue(new WorkspaceApiError(409, {
      code: 'DESTINATION_CHANGED',
      message: '目标文件已变化',
      mutation_state: 'failed',
      outcome: 'not_applied',
    }));
    const ref = createRef<FilePreviewHandle>();
    render(<FilePreview ref={ref} inline refreshInPlace file={current}
      sessionId={owner.ownerSessionId} onClose={() => {}} />);
    const editor = await findMarkdownEditor();
    fireEvent.change(editor, { target: { value: '# 未保存草稿' } });

    await act(async () => {
      await ref.current!.saveDirty(owner);
    });

    expect(await screen.findByTestId('workspace-draft-loss-notice')).toHaveTextContent(
      '刚才的修改未保存，已恢复到最近保存版本。',
    );
    await waitFor(async () => {
      expect(await findMarkdownEditor()).toHaveValue('# 服务端恢复正文');
    });
    expect(ref.current!.isDirty(owner)).toBe(false);
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it('电子表格自动合并后安装固定版本正文再接纳新基线', async () => {
    const owner = { ownerSessionId: 'workspace:persistent', ownerEpoch: 0 };
    const current: FileInfo = {
      ...markdownFile, source: 'workspace', entry_id: 'merged-sheet', path: 'merged-sheet.xlsx',
      name: 'merged-sheet.xlsx', type: 'xlsx', revision: 1, version_id: 'sheet-base',
    };
    vi.stubGlobal('fetch', vi.fn()
      .mockResolvedValueOnce({ ok: true, arrayBuffer: async () => new Uint8Array([1]).buffer })
      .mockResolvedValueOnce({ ok: true, arrayBuffer: async () => new Uint8Array([1, 2, 3]).buffer }));
    const { workspaceApi } = await import('../../services/workspaceApi');
    vi.spyOn(workspaceApi, 'updateContent').mockResolvedValue({
      status: 'UPDATED', mutation_id: 'sheet-save', auto_merged: true,
      entry: {
        entry_id: current.entry_id!, parent_id: null, name: current.name, kind: 'file', path: current.path,
        size_bytes: 3, mime_type: null, sha256: 'sha', revision: 3, current_version_id: 'sheet-merged',
        status: 'active', created_at: current.modified, updated_at: '2026-08-30T00:00:01Z',
      },
    });
    const ref = createRef<FilePreviewHandle>();
    const updated = vi.fn();
    render(<FilePreview ref={ref} inline file={current} sessionId={owner.ownerSessionId}
      onFileUpdated={updated} onClose={() => {}} />);
    await screen.findByTestId('spreadsheet-editor');
    fireEvent.click(screen.getByRole('button', { name: '模拟编辑单元格' }));
    await act(async () => { await ref.current!.saveDirty(owner); });
    expect(screen.getByTestId('spreadsheet-editor')).toHaveAttribute('data-source-size', '3');
    expect(ref.current!.isDirty(owner)).toBe(false);
    expect(updated.mock.calls[0][0]).toMatchObject({ version_id: 'sheet-merged', revision: '3' });
  });

  it('current 与 captured 是不同 owner identity，切到快照不能短暂沿用 current 正文', async () => {
    const capturedResponse = deferred<{ ok: boolean; text: () => Promise<string> }>();
    const fetchMock = vi.fn()
      .mockResolvedValueOnce({ ok: true, text: () => Promise.resolve('# 当前内容') })
      .mockImplementationOnce(() => capturedResponse.promise);
    vi.stubGlobal('fetch', fetchMock);
    const current: FileInfo = {
      ...markdownFile,
      source: 'workspace', entry_id: 'entry-isolated', workspace_path: 'report.md',
      version_id: 'version-current', revision: 3, content_mode: 'current',
    };
    const { rerender } = render(
      <FilePreview inline refreshInPlace file={current} sessionId="workspace:persistent" onClose={() => {}} />,
    );
    expect(await findMarkdownEditor()).toHaveValue('# 当前内容');

    rerender(
      <FilePreview
        inline
        refreshInPlace
        readOnly
        file={{ ...current, content_mode: 'captured', version_id: 'version-captured', assistant_ref_id: 'workspace:entry-isolated:version-captured' }}
        sessionId="workspace-version:version-captured"
        onClose={() => {}}
      />,
    );

    expect(screen.getByTestId('file-preview-loading')).toBeInTheDocument();
    expect(screen.queryByText('当前内容')).not.toBeInTheDocument();
  });

  it('工作区重命名保留同一内容版本和未保存 Markdown 草稿', async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      text: () => Promise.resolve('# 旧内容'),
    });
    vi.stubGlobal('fetch', fetchMock);
    const onSaveMarkdownFile = vi.fn();
    const renderPreview = (file: FileInfo) => (
      <FilePreview
        inline
        refreshInPlace
        file={file}
        sessionId="workspace:persistent"
        onClose={() => {}}
        onSaveMarkdownFile={onSaveMarkdownFile}
      />
    );
    const original = {
      ...markdownFile,
      source: 'workspace' as const,
      entry_id: 'entry-rename',
      version_id: 'version-1',
      revision: 1,
    };
    const { rerender } = render(renderPreview(original));
    const editor = await findMarkdownEditor();
    fireEvent.change(editor, { target: { value: '# 我的草稿' } });

    rerender(renderPreview({
      ...original,
      name: 'renamed.md',
      path: 'renamed.md',
      workspace_path: 'renamed.md',
      revision: 2,
      modified: '2026-08-28T10:00:00Z',
    }));

    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(screen.getByRole('textbox', { name: 'Markdown 所见即所得编辑器' })).toHaveValue('# 我的草稿');
    expect(screen.queryByText('正在更新文件')).not.toBeInTheDocument();
  });

  it('Session Markdown 直接进入所见即所得编辑器，不显示阅读模式栏', async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      text: () => Promise.resolve('# 标题\n\n正文'),
    });
    vi.stubGlobal('fetch', fetchMock);

    render(
      <FilePreview
        file={{ ...markdownFile, path: 'reports/report.md' }}
        sessionId="session-markdown"
        onClose={() => {}}
      />,
    );

    expect(await findMarkdownEditor()).toHaveValue('# 标题\n\n正文');
    expect(screen.queryByRole('button', { name: '编辑 Markdown' })).not.toBeInTheDocument();
    expect(screen.queryByRole('toolbar', { name: 'Markdown 阅读工具' })).not.toBeInTheDocument();
    const toolbarToggle = screen.getByRole('button', { name: '展开 Markdown 格式工具' });
    fireEvent.click(toolbarToggle);
    expect(toolbarToggle).toHaveAttribute('aria-expanded', 'true');
  });

  it('自身 PUT 尚未回执时重开读取到新版本，不误判冲突，回执更新新编辑器', async () => {
    const file = { ...markdownFile, revision: 'v1:100:1', edit_base_token: 'base-1' };
    const updated = { ...file, revision: 'v1:110:2', size: 110, modified: '2026-09-01T02:00:00Z', edit_base_token: 'base-2' };
    const pendingSave = deferred<FileInfo>();
    updateSessionMarkdownMock.mockReturnValueOnce(pendingSave.promise);
    const queued = queueSessionMarkdownDraft('session-reopen', file, '# 最新正文');
    const saving = flushSessionDraft(queued.key);
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: true,
      headers: new Headers({ 'X-Session-Edit-Base': 'base-2', 'X-Session-File-Revision': updated.revision }),
      text: async () => '# 最新正文',
    }));
    render(<FilePreview inline file={updated} sessionId="session-reopen" onClose={() => {}} />);
    expect(await findMarkdownEditor()).toHaveValue('# 最新正文');
    expect(screen.queryByTestId('retained-session-draft')).not.toBeInTheDocument();
    await act(async () => { pendingSave.resolve(updated); await saving; });
    expect(screen.queryByTestId('retained-session-draft')).not.toBeInTheDocument();
    expect(await findMarkdownEditor()).toHaveValue('# 最新正文');
  });

  it('revision conflict 默认显示权威正文并持续提供下载或丢弃 retained 草稿', async () => {
    const file = {
      ...markdownFile,
      session_id: 'session-conflict-ui',
      revision: 'v1:100:1',
    };
    updateSessionMarkdownMock.mockRejectedValueOnce({
      status: 409,
      code: 'SESSION_FILE_REVISION_CONFLICT',
    });
    const queued = queueSessionMarkdownDraft('session-conflict-ui', file, '# 本地冲突草稿');
    await expect(flushSessionDraft(queued.key)).rejects.toBeTruthy();
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: true,
      text: () => Promise.resolve('# 权威内容'),
    }));

    render(<FilePreview inline file={file} sessionId="session-conflict-ui" onClose={() => {}} />);

    expect(await screen.findByText('权威内容')).toBeInTheDocument();
    expect(screen.queryByRole('textbox', { name: 'Markdown 所见即所得编辑器' })).not.toBeInTheDocument();
    expect(screen.getByTestId('retained-session-draft')).toHaveTextContent('不会覆盖当前文件');
    const anchorClick = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {});
    fireEvent.click(screen.getByRole('button', { name: '下载草稿' }));
    expect(URL.createObjectURL).toHaveBeenCalled();
    anchorClick.mockRestore();

    fireEvent.click(screen.getByRole('button', { name: '丢弃草稿' }));
    await waitFor(() => expect(screen.queryByTestId('retained-session-draft')).not.toBeInTheDocument());
    expect(await findMarkdownEditor()).toHaveValue('# 权威内容');
  });

  it('captured/read-only Markdown 不读取同路径 Session outbox 草稿', async () => {
    const file = {
      ...markdownFile,
      session_id: 'session-captured',
      revision: 'v1:100:1',
      content_mode: 'captured' as const,
      snapshot_path: '.assistant-artifacts/round/report.md',
    };
    queueSessionMarkdownDraft('session-captured', file, '# 不应出现的草稿');
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: true,
      text: () => Promise.resolve('# 冻结快照'),
    }));

    render(<FilePreview inline readOnly file={file} sessionId="session-captured" onClose={() => {}} />);

    expect(await screen.findByText('冻结快照')).toBeInTheDocument();
    expect(screen.queryByText('不应出现的草稿')).not.toBeInTheDocument();
    expect(screen.queryByTestId('retained-session-draft')).not.toBeInTheDocument();
  });

  it('所有 Session 文件类型都显示存入工作区，custom source 默认不显示', async () => {
    const unsupportedFile: FileInfo = {
      name: 'archive.bin',
      path: 'archive.bin',
      size: 42,
      modified: '2026-04-16T18:30:00Z',
      revision: 'v1:42:1',
      type: 'bin',
    };
    const { rerender } = render(
      <FilePreview canSaveToWorkspace file={unsupportedFile} sessionId="session-files" onClose={() => {}} />,
    );
    expect(screen.getByRole('button', { name: '将当前会话文件存入工作区' })).toBeInTheDocument();

    rerender(
      <FilePreview
        file={unsupportedFile}
        sessionId="cron-run"
        previewUrlBuilder={() => '/api/cron/runs/run/files/archive.bin'}
        onClose={() => {}}
      />,
    );
    expect(screen.queryByRole('button', { name: '将当前会话文件存入工作区' })).not.toBeInTheDocument();
  });

  it('Workspace-origin 预览只显示在工作区打开，不重复显示存入工作区', () => {
    const onOpenWorkspace = vi.fn();
    render(
      <FilePreview
        file={{ name: 'workspace.bin', path: '.workspace-snapshots/entry/1/workspace.bin', size: 10, modified: 'now', type: 'bin', source: 'workspace', entry_id: 'entry', workspace_path: 'workspace.bin', revision: 1 }}
        sessionId="session-1"
        readOnly
        onOpenWorkspace={onOpenWorkspace}
        canSaveToWorkspace={false}
        onClose={() => {}}
      />,
    );
    fireEvent.click(screen.getByRole('button', { name: '在工作区打开' }));
    expect(onOpenWorkspace).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole('button', { name: '将当前会话文件存入工作区' })).not.toBeInTheDocument();
  });

  it('旧历史附件缺 revision 时先按完整路径刷新 metadata，再打开选择器', async () => {
    const legacyFile: FileInfo = {
      name: 'legacy.bin', path: 'reports/legacy.bin', size: 42,
      modified: '2026-04-16T18:30:00Z', type: 'bin',
    };
    getSessionFilesMock.mockResolvedValueOnce({
      files: [{ ...legacyFile, revision: 'v1:42:123' }], total: 1,
    });
    render(<FilePreview canSaveToWorkspace file={legacyFile} sessionId="legacy-session" onClose={() => {}} />);
    fireEvent.click(screen.getByRole('button', { name: '将当前会话文件存入工作区' }));

    expect(await screen.findByRole('dialog', { name: '存入工作区' })).toBeInTheDocument();
    expect(getSessionFilesMock).toHaveBeenCalledWith('legacy-session', 'reports');
  });

  it('刷新 revision 时发现文件已变化则阻止复制旧预览', async () => {
    const legacyFile: FileInfo = {
      name: 'legacy.bin', path: 'legacy.bin', size: 42,
      modified: '2026-04-16T18:30:00Z', type: 'bin',
    };
    getSessionFilesMock.mockResolvedValueOnce({
      files: [{ ...legacyFile, revision: 'v1:43:124', size: 43 }], total: 1,
    });
    render(<FilePreview canSaveToWorkspace file={legacyFile} sessionId="legacy-session" onClose={() => {}} />);
    fireEvent.click(screen.getByRole('button', { name: '将当前会话文件存入工作区' }));

    expect(await screen.findByText('会话文件已被更新，请刷新预览后再存入工作区。')).toBeInTheDocument();
    expect(screen.queryByRole('dialog', { name: '存入工作区' })).not.toBeInTheDocument();
  });

  it('存入工作区前先等待 dirty Markdown 保存，并使用保存后的 revision', async () => {
    const importFile: FileInfo = {
      ...markdownFile,
      name: 'import-report.md',
      path: 'import-report.md',
      modified: '2026-04-16T18:39:00Z',
      revision: 'v1:100:1',
    };
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: true,
      text: () => Promise.resolve('# 初始内容'),
    }));
    let resolveSave!: (file: FileInfo) => void;
    updateSessionMarkdownMock.mockImplementationOnce(() => new Promise<FileInfo>((resolve) => {
      resolveSave = resolve;
    }));
    render(
      <FilePreview
        canSaveToWorkspace
        file={importFile}
        sessionId="session-markdown"
        onClose={() => {}}
      />,
    );
    const editor = await findMarkdownEditor();
    fireEvent.change(editor, { target: { value: '# 保存后再导入' } });
    fireEvent.click(screen.getByRole('button', { name: '将当前会话文件存入工作区' }));

    expect(updateSessionMarkdownMock).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole('dialog', { name: '存入工作区' })).not.toBeInTheDocument();

    await act(async () => {
      resolveSave({
        ...importFile,
        revision: 'v1:120:2',
        size: 120,
        modified: '2026-04-16T18:31:00Z',
      });
    });

    expect(await screen.findByRole('dialog', { name: '存入工作区' })).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '确定' }));
    await waitFor(() => expect(workspacePostMock).toHaveBeenCalledWith(
      '/workspace/imports/session-file',
      expect.objectContaining({ source_revision: 'v1:120:2' }),
    ));
  });

  it('显式只读的 Session Markdown 只渲染预览且不初始化编辑器', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: true,
      text: () => Promise.resolve('# 只读附件'),
    }));

    render(
      <FilePreview
        readOnly
        file={markdownFile}
        sessionId="session-markdown"
        onClose={() => {}}
      />,
    );

    expect(await screen.findByRole('heading', { name: '只读附件' })).toBeInTheDocument();
    expect(screen.queryByRole('textbox', { name: 'Markdown 所见即所得编辑器' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '展开 Markdown 格式工具' })).not.toBeInTheDocument();
    expect(markdownAdapterInitializationCount).toBe(0);
    expect(updateSessionMarkdownMock).not.toHaveBeenCalled();
  });

  it('停止编辑后按当前文件版本自动保存 Markdown', async () => {
    expect(MARKDOWN_AUTOSAVE_DELAY_MS).toBe(300);
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: true,
      text: () => Promise.resolve('# 初始内容'),
    }));
    updateSessionMarkdownMock.mockResolvedValue({
      ...markdownFile,
      size: 18,
      modified: '2026-04-16T18:31:00Z',
    });
    const onFileUpdated = vi.fn();

    render(
      <FilePreview
        file={markdownFile}
        sessionId="session-markdown"
        onClose={() => {}}
        onFileUpdated={onFileUpdated}
      />,
    );

    const editor = await findMarkdownEditor();
    fireEvent.change(editor, { target: { value: '# 已修改内容' } });
    expect(screen.queryByText('等待自动保存')).not.toBeInTheDocument();
    expect(screen.queryByText('正在保存…')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '保存 Markdown' })).not.toBeInTheDocument();

    await waitFor(() => {
      expect(updateSessionMarkdownMock).toHaveBeenCalledWith(
        'session-markdown',
        expect.objectContaining({ size: 100, modified: '2026-04-16T18:30:00Z' }),
        '# 已修改内容',
        expect.any(String),
      );
      expect(onFileUpdated).toHaveBeenCalledWith(expect.objectContaining({ size: 18 }));
      expect(screen.queryByText('已自动保存')).not.toBeInTheDocument();
    }, { timeout: MARKDOWN_AUTOSAVE_DELAY_MS + 1800 });
  });

  it('关闭请求会绕过静默窗口立即保存 Markdown', async () => {
    const closeFile: FileInfo = {
      ...markdownFile,
      name: 'close-save.md',
      path: 'close-save.md',
      modified: '2026-04-16T18:35:00Z',
    };
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: true,
      text: () => Promise.resolve('# 初始内容'),
    }));
    updateSessionMarkdownMock.mockResolvedValue({
      ...closeFile,
      size: 18,
      modified: '2026-04-16T18:36:00Z',
    });

    const { rerender } = render(
      <FilePreview
        file={closeFile}
        sessionId="session-markdown"
        onClose={() => {}}
      />,
    );
    const editor = await findMarkdownEditor();
    fireEvent.change(editor, { target: { value: '# 关闭前保存' } });

    rerender(
      <FilePreview
        file={closeFile}
        sessionId="session-markdown"
        onClose={() => {}}
        saveRequestNonce={1}
      />,
    );

    await waitFor(() => {
      expect(updateSessionMarkdownMock).toHaveBeenCalledWith(
        'session-markdown',
        expect.objectContaining({ modified: '2026-04-16T18:35:00Z' }),
        '# 关闭前保存',
        expect.any(String),
      );
    }, { timeout: 700 });
  });

  it('Session 切换 flush 会在保存期间继续输入时串行保存到最新 revision', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: true,
      text: () => Promise.resolve('# 初始内容'),
    }));
    let resolveFirstSave!: (file: FileInfo) => void;
    updateSessionMarkdownMock
      .mockImplementationOnce(() => new Promise<FileInfo>((resolve) => {
        resolveFirstSave = resolve;
      }))
      .mockResolvedValueOnce({
        ...markdownFile,
        size: 30,
        modified: '2026-04-16T18:32:00Z',
      });
    const previewRef = createRef<FilePreviewHandle>();

    render(
      <FilePreview
        ref={previewRef}
        file={markdownFile}
        sessionId="session-markdown"
        ownerEpoch={7}
        onClose={() => {}}
      />,
    );
    const editor = await findMarkdownEditor();
    fireEvent.change(editor, { target: { value: '# 第一版' } });

    let flushPromise!: ReturnType<FilePreviewHandle['saveDirty']>;
    await act(async () => {
      flushPromise = previewRef.current!.saveDirty({
        ownerSessionId: 'session-markdown',
        ownerEpoch: 7,
      });
      await Promise.resolve();
    });
    expect(updateSessionMarkdownMock).toHaveBeenCalledTimes(1);

    fireEvent.change(editor, { target: { value: '# 保存期间的最新版' } });
    await act(async () => {
      resolveFirstSave({
        ...markdownFile,
        size: 12,
        modified: '2026-04-16T18:31:00Z',
      });
      await Promise.resolve();
    });

    const result = await flushPromise;
    expect(result).toMatchObject({
      ownerSessionId: 'session-markdown',
      ownerEpoch: 7,
      ok: true,
      stale: false,
    });
    expect(updateSessionMarkdownMock).toHaveBeenCalledTimes(2);
    expect(updateSessionMarkdownMock).toHaveBeenNthCalledWith(
      2,
      'session-markdown',
      expect.objectContaining({ modified: '2026-04-16T18:31:00Z' }),
      '# 保存期间的最新版',
      expect.any(String),
    );
  });

  it('Markdown 保存期间的新输入会使用响应版本继续串行自动保存', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: true,
      text: () => Promise.resolve('# 初始内容'),
    }));
    let resolveFirstSave!: (file: FileInfo) => void;
    updateSessionMarkdownMock
      .mockImplementationOnce(() => new Promise<FileInfo>((resolve) => {
        resolveFirstSave = resolve;
      }))
      .mockResolvedValueOnce({
        ...markdownFile,
        size: 24,
        modified: '2026-04-16T18:32:00Z',
      });

    render(
      <FilePreview
        file={markdownFile}
        sessionId="session-markdown"
        onClose={() => {}}
      />,
    );

    const editor = await findMarkdownEditor();
    fireEvent.change(editor, { target: { value: '# 第一版' } });
    await waitFor(() => expect(updateSessionMarkdownMock).toHaveBeenCalledTimes(1), {
      timeout: MARKDOWN_AUTOSAVE_DELAY_MS + 1800,
    });

    fireEvent.change(editor, { target: { value: '# 保存期间的新版本' } });
    resolveFirstSave({
      ...markdownFile,
      size: 12,
      modified: '2026-04-16T18:31:00Z',
    });

    await waitFor(() => expect(updateSessionMarkdownMock).toHaveBeenCalledTimes(2), {
      timeout: MARKDOWN_AUTOSAVE_DELAY_MS + 2200,
    });
    expect(updateSessionMarkdownMock).toHaveBeenNthCalledWith(
      2,
      'session-markdown',
      expect.objectContaining({ size: 12, modified: '2026-04-16T18:31:00Z' }),
      '# 保存期间的新版本',
      expect.any(String),
    );
    expect(screen.queryByText('等待自动保存')).not.toBeInTheDocument();
    expect(screen.queryByText('正在保存…')).not.toBeInTheDocument();
    expect(screen.queryByText('已自动保存')).not.toBeInTheDocument();
  });

  it('Markdown 自动保存后的父级元数据刷新不会重载编辑器或重置滚动位置', async () => {
    const refreshFile: FileInfo = {
      ...markdownFile,
      name: 'no-refresh.md',
      path: 'no-refresh.md',
      modified: '2026-04-16T18:40:00Z',
    };
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      text: () => Promise.resolve('# 初始内容'),
    });
    vi.stubGlobal('fetch', fetchMock);
    updateSessionMarkdownMock.mockResolvedValue({
      ...refreshFile,
      size: 18,
      modified: '2026-04-16T18:41:00Z',
    });

    function MetadataRefreshingPreview() {
      const [currentFile, setCurrentFile] = useState(refreshFile);
      return (
        <FilePreview
          file={currentFile}
          sessionId="session-markdown"
          onClose={() => {}}
          onFileUpdated={setCurrentFile}
        />
      );
    }

    render(<MetadataRefreshingPreview />);
    const editor = await findMarkdownEditor();
    const initializationCountBeforeSave = markdownAdapterInitializationCount;
    editor.scrollTop = 160;
    fireEvent.change(editor, { target: { value: '# 已修改内容' } });

    await waitFor(() => expect(updateSessionMarkdownMock).toHaveBeenCalledTimes(1), {
      timeout: MARKDOWN_AUTOSAVE_DELAY_MS + 1800,
    });
    expect(screen.queryByText('等待自动保存')).not.toBeInTheDocument();
    expect(screen.queryByText('正在保存…')).not.toBeInTheDocument();
    expect(screen.queryByText('已自动保存')).not.toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(markdownAdapterInitializationCount).toBe(initializationCountBeforeSave);
    expect(screen.getByRole('textbox', { name: 'Markdown 所见即所得编辑器' })).toBe(editor);
    expect(editor.scrollTop).toBe(160);
  });

  it('shows specific download failure detail', async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      text: () => Promise.resolve('# hello preview'),
    });
    vi.stubGlobal('fetch', fetchMock);

    const { apiService } = await import('../../services/api');
    vi.mocked(apiService.downloadFile).mockRejectedValueOnce(new Error('文件不存在或尚未生成'));

    render(
      <FilePreview
        file={markdownFile}
        sessionId="session-1"
        onClose={() => {}}
      />,
    );

    expect(await findMarkdownEditor()).toHaveValue('# hello preview');

    fireEvent.click(screen.getByRole('button', { name: '下载文件' }));

    await waitFor(() => {
      expect(screen.getByText('下载文件失败：文件不存在或尚未生成')).toBeInTheDocument();
    });
  });

  it('Markdown 下载按钮直接下载原文件，不显示格式菜单', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: true,
      text: () => Promise.resolve('# 正文'),
    }));
    const { apiService } = await import('../../services/api');
    vi.mocked(apiService.downloadFile).mockResolvedValueOnce(undefined);
    render(<FilePreview file={markdownFile} sessionId="session-1" onClose={() => {}} />);
    await findMarkdownEditor();

    fireEvent.click(screen.getByRole('button', { name: '下载文件' }));

    await waitFor(() => expect(apiService.downloadFile).toHaveBeenCalledWith('session-1', 'report.md'));
    expect(screen.queryByRole('menu')).not.toBeInTheDocument();
    expect(updateSessionMarkdownMock).not.toHaveBeenCalled();
  });

  it('通过 sandbox srcDoc 渲染 HTML 并在 load 后结束 loading', async () => {
    const htmlFile: FileInfo = {
      name: 'preview.html',
      path: 'preview.html',
      size: 128,
      modified: '2026-04-22T10:00:00Z',
      type: 'html',
      is_directory: false,
    };

    const html = '<!doctype html><html><body><h1>HTML Preview</h1></body></html>';
    const { createObjectURLMock } = mockObjectUrlApis('blob:html-preview');
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      text: () => Promise.resolve(html),
    });
    vi.stubGlobal('fetch', fetchMock);

    render(
      <FilePreview
        file={htmlFile}
        sessionId="session-1"
        onClose={() => {}}
      />,
    );

    const iframe = await screen.findByTitle('preview.html');
    expect(createObjectURLMock).not.toHaveBeenCalled();
    expect(iframe).not.toHaveAttribute('src');
    expect(iframe).toHaveAttribute('srcdoc', html);
    expect(iframe).toHaveAttribute('sandbox', 'allow-scripts');
    expect(iframe).not.toHaveAttribute('sandbox', expect.stringContaining('allow-same-origin'));

    fireEvent.load(iframe);
    await waitFor(() => {
      expect(screen.queryByTestId('html-iframe-loading')).not.toBeInTheDocument();
    });
  });

  it('HTML srcDoc deadline 到期时应该进入确定的 timeout 回调', () => {
    vi.useFakeTimers();
    const onTimeout = vi.fn();
    scheduleHtmlInlinePreviewTimeout(onTimeout);

    act(() => {
      vi.advanceTimersByTime(HTML_INLINE_PREVIEW_TIMEOUT_MS);
    });

    expect(onTimeout).toHaveBeenCalledTimes(1);
  });

  it('loads image preview through segmented encoded sandbox URL', async () => {
    const imageFile: FileInfo = {
      name: '中文 图.png',
      path: '中文 图.png',
      size: 256,
      modified: '2026-06-24T10:00:00Z',
      type: 'image/png',
      is_directory: false,
    };
    const { createObjectURLMock } = mockObjectUrlApis('blob:image-preview');
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      blob: () => Promise.resolve(new Blob(['png'], { type: 'image/png' })),
    });
    vi.stubGlobal('fetch', fetchMock);

    render(
      <FilePreview
        file={imageFile}
        sessionId="session-1"
        onClose={() => {}}
      />,
    );

    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith('/api/sessions/session-1/files/%E4%B8%AD%E6%96%87%20%E5%9B%BE.png?preview=true', expect.objectContaining({
        headers: { Authorization: 'Bearer test-token' },
        signal: expect.any(AbortSignal),
      }));
      expect(createObjectURLMock).toHaveBeenCalledTimes(1);
    });

    expect(screen.getByAltText('中文 图.png')).toHaveAttribute('src', 'blob:image-preview');
  });

  it('切换 HTML 文件时更新 srcDoc 且内嵌预览不创建 Blob URL', async () => {
    const htmlFileA: FileInfo = {
      name: 'first.html',
      path: 'first.html',
      size: 96,
      modified: '2026-04-22T10:10:00Z',
      type: 'html',
      is_directory: false,
    };
    const htmlFileB: FileInfo = {
      name: 'second.html',
      path: 'second.html',
      size: 112,
      modified: '2026-04-22T10:11:00Z',
      type: 'html',
      is_directory: false,
    };

    const { createObjectURLMock, revokeObjectURLMock } = mockObjectUrlApis('blob:first', 'blob:second');
    const fetchMock = vi.fn()
      .mockResolvedValueOnce({
        ok: true,
        text: () => Promise.resolve('<html><body>first</body></html>'),
      })
      .mockResolvedValueOnce({
        ok: true,
        text: () => Promise.resolve('<html><body>second</body></html>'),
      });
    vi.stubGlobal('fetch', fetchMock);

    const { rerender, unmount } = render(
      <FilePreview
        file={htmlFileA}
        sessionId="session-2"
        onClose={() => {}}
      />,
    );

    expect(await screen.findByTitle('first.html')).toHaveAttribute(
      'srcdoc',
      '<html><body>first</body></html>',
    );

    rerender(
      <FilePreview
        file={htmlFileB}
        sessionId="session-2"
        onClose={() => {}}
      />,
    );

    expect(await screen.findByTitle('second.html')).toHaveAttribute(
      'srcdoc',
      '<html><body>second</body></html>',
    );
    expect(createObjectURLMock).not.toHaveBeenCalled();
    expect(revokeObjectURLMock).not.toHaveBeenCalled();

    unmount();
    expect(revokeObjectURLMock).not.toHaveBeenCalled();
  });

  it('用完整电子表格编辑器打开 XLSX，并在修改后自动保存且刷新时间', async () => {
    expect(SPREADSHEET_AUTOSAVE_DELAY_MS).toBe(300);
    const xlsxFile: FileInfo = {
      name: 'metrics.xlsx',
      path: 'metrics.xlsx',
      size: 2048,
      modified: '2026-04-22T10:20:00Z',
      type: 'xlsx',
      is_directory: false,
    };
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      arrayBuffer: () => Promise.resolve(new ArrayBuffer(8)),
    });
    vi.stubGlobal('fetch', fetchMock);
    updateSessionSpreadsheetMock.mockResolvedValue({
      ...xlsxFile,
      size: 4096,
      modified: '2026-04-22T10:21:00Z',
    });
    const onFileUpdated = vi.fn();

    render(
      <FilePreview
        file={xlsxFile}
        sessionId="session-3"
        onClose={() => {}}
        onFileUpdated={onFileUpdated}
      />,
    );

    expect(await screen.findByTestId('spreadsheet-editor')).toHaveAttribute('data-readonly', 'false');
    expect(screen.getByTestId('spreadsheet-modified-time')).toHaveTextContent('最近修改');

    fireEvent.click(screen.getByRole('button', { name: '模拟编辑单元格' }));
    expect(screen.queryByText('等待自动保存')).not.toBeInTheDocument();
    expect(screen.queryByText('正在保存…')).not.toBeInTheDocument();
    await waitFor(() => {
      expect(updateSessionSpreadsheetMock).toHaveBeenCalledWith(
        'session-3',
        expect.objectContaining({ size: 2048, modified: '2026-04-22T10:20:00Z' }),
        expect.any(ArrayBuffer),
        expect.any(String),
      );
      expect(onFileUpdated).toHaveBeenCalledWith(expect.objectContaining({ size: 4096 }));
      expect(screen.queryByText('已自动保存')).not.toBeInTheDocument();
    }, { timeout: SPREADSHEET_AUTOSAVE_DELAY_MS + 1800 });
  });

  it('AI 更新工作区文件时丢弃旧自动保存结果，并让后续编辑基于最新 revision 保存', async () => {
    const oldFile: FileInfo = {
      name: 'workspace.xlsx',
      path: 'workspace.xlsx',
      size: 2048,
      modified: '2026-08-27T11:40:00Z',
      revision: '1',
      type: 'xlsx',
      is_directory: false,
    };
    const latestFile: FileInfo = {
      ...oldFile,
      size: 8192,
      modified: '2026-08-27T11:56:00Z',
      revision: '12',
    };
    const fetchMock = vi.fn()
      .mockResolvedValueOnce({ ok: true, arrayBuffer: () => Promise.resolve(new ArrayBuffer(8)) })
      .mockResolvedValueOnce({ ok: true, arrayBuffer: () => Promise.resolve(new ArrayBuffer(16)) });
    vi.stubGlobal('fetch', fetchMock);

    let rejectOldSave!: (reason: Error) => void;
    const onSaveSpreadsheetFile = vi.fn()
      .mockImplementationOnce(() => new Promise<FileInfo>((_resolve, reject) => {
        rejectOldSave = reject;
      }))
      .mockResolvedValueOnce({
        ...latestFile,
        modified: '2026-08-27T11:57:00Z',
        revision: '13',
      });

    const renderPreview = (file: FileInfo) => (
      <FilePreview
        file={file}
        sessionId="workspace:persistent"
        onClose={() => {}}
        previewUrlBuilder={() => '/api/workspace/entries/entry-1/content?preview=true'}
        onSaveSpreadsheetFile={onSaveSpreadsheetFile}
      />
    );
    const { rerender } = render(renderPreview(oldFile));

    fireEvent.click(await screen.findByRole('button', { name: '模拟编辑单元格' }));
    await waitFor(() => expect(onSaveSpreadsheetFile).toHaveBeenCalledTimes(1), {
      timeout: SPREADSHEET_AUTOSAVE_DELAY_MS + 1000,
    });

    rerender(renderPreview(latestFile));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
    await act(async () => {
      rejectOldSave(new Error('目标文件已被其他操作修改'));
      await Promise.resolve();
    });

    expect(screen.queryByText('保存失败')).not.toBeInTheDocument();
    fireEvent.click(await screen.findByRole('button', { name: '模拟编辑单元格' }));
    await waitFor(() => expect(onSaveSpreadsheetFile).toHaveBeenCalledTimes(2), {
      timeout: SPREADSHEET_AUTOSAVE_DELAY_MS + 1000,
    });
    expect(onSaveSpreadsheetFile).toHaveBeenLastCalledWith(
      expect.objectContaining({ revision: '12' }),
      expect.any(ArrayBuffer),
    );
  });

  it('显式只读的 Session XLSX 不提供编辑或写回', async () => {
    const xlsxFile: FileInfo = {
      name: 'attachment.xlsx',
      path: 'attachment.xlsx',
      size: 2048,
      modified: '2026-08-26T10:00:00Z',
      type: 'xlsx',
      is_directory: false,
    };
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: true,
      arrayBuffer: () => Promise.resolve(new ArrayBuffer(8)),
    }));

    render(<FilePreview readOnly file={xlsxFile} sessionId="session-xlsx" onClose={() => {}} />);

    expect(await screen.findByTestId('spreadsheet-editor')).toHaveAttribute('data-readonly', 'true');
    expect(screen.getByRole('status')).toHaveTextContent('只读');
    expect(screen.queryByRole('button', { name: '模拟编辑单元格' })).not.toBeInTheDocument();
    expect(updateSessionSpreadsheetMock).not.toHaveBeenCalled();
  });

  it('用 Univer 打开 ET，但强制只读且不触发写回', async () => {
    const etFile: FileInfo = {
      name: '预算.et',
      path: '预算.et',
      size: 1024,
      modified: '2026-08-26T07:30:00Z',
      type: 'et',
      is_directory: false,
    };
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: true,
      arrayBuffer: () => Promise.resolve(new ArrayBuffer(8)),
    }));

    render(<FilePreview file={etFile} sessionId="session-et" onClose={() => {}} />);

    expect(await screen.findByTestId('spreadsheet-editor')).toHaveAttribute('data-readonly', 'true');
    expect(screen.getByRole('status')).toHaveTextContent('只读');
    expect(screen.queryByRole('button', { name: '模拟编辑单元格' })).not.toBeInTheDocument();
    expect(updateSessionSpreadsheetMock).not.toHaveBeenCalled();
  });

  it('renders a ZIP directory inline without opening a new page', async () => {
    const zip = new JSZip();
    zip.file('docs/readme.md', '# Read me');
    zip.file('data/report.csv', 'name,value\nA,1');
    const archive = await zip.generateAsync({ type: 'arraybuffer' });
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      arrayBuffer: () => Promise.resolve(archive),
    });
    vi.stubGlobal('fetch', fetchMock);

    render(
      <FilePreview
        inline
        file={{
          name: 'bundle.zip',
          path: 'bundle.zip',
          size: archive.byteLength,
          modified: '2026-07-27T12:00:00Z',
          type: 'zip',
          is_directory: false,
        }}
        sessionId="session-zip"
        onClose={() => {}}
      />,
    );

    expect(await screen.findByText('压缩包目录')).toBeInTheDocument();
    expect(screen.getByText('docs/readme.md')).toBeInTheDocument();
    expect(screen.getByText('data/report.csv')).toBeInTheDocument();
    expect(screen.getByText(/只读/)).toBeInTheDocument();
  });

  it('弹窗被阻止时回收 HTML Blob 并显示提示', async () => {
    const htmlFile: FileInfo = {
      name: 'interactive.html',
      path: 'interactive.html',
      size: 160,
      modified: '2026-08-26T01:00:00Z',
      type: 'html',
      is_directory: false,
    };
    const { createObjectURLMock, revokeObjectURLMock } = mockObjectUrlApis('blob:content', 'blob:wrapper');
    const openMock = vi.spyOn(window, 'open').mockReturnValue(null);
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: true,
      text: () => Promise.resolve('<!doctype html><title>Demo</title><script>document.body.textContent="ok"</script>'),
    }));

    render(<FilePreview file={htmlFile} sessionId="session-html" onClose={() => {}} />);

    await screen.findByTitle('interactive.html');
    expect(createObjectURLMock).not.toHaveBeenCalled();
    fireEvent.click(screen.getByTitle('在新标签页中查看'));

    expect(createObjectURLMock).toHaveBeenCalledTimes(2);
    expect(openMock).toHaveBeenCalledWith('blob:wrapper', '_blank');
    expect(revokeObjectURLMock).toHaveBeenCalledWith('blob:content');
    expect(revokeObjectURLMock).toHaveBeenCalledWith('blob:wrapper');
    expect(await screen.findByText('无法在新标签页中打开 HTML')).toBeInTheDocument();
    const wrapperBlob = createObjectURLMock.mock.calls[1][0] as Blob;
    const wrapperText = await new Promise<string>((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(String(reader.result));
      reader.onerror = () => reject(reader.error);
      reader.readAsText(wrapperBlob);
    });
    expect(wrapperText).toContain('sandbox="allow-scripts"');
    expect(wrapperText).not.toContain('allow-same-origin');
    expect(wrapperText).toContain('src="blob:content"');
  });

  it('首次工作区 PPTX 尚未就绪时保持加载，软提示不会提前降级或中止转换', async () => {
    vi.useFakeTimers();
    const firstResponse = deferred<{
      ok: boolean;
      headers: { get: () => string };
      blob: () => Promise<Blob>;
    }>();
    const secondResponse = deferred<{
      ok: boolean;
      headers: { get: () => string };
      blob: () => Promise<Blob>;
    }>();
    const fetchMock = vi.fn()
      .mockImplementationOnce(() => firstResponse.promise)
      .mockImplementationOnce(() => secondResponse.promise);
    vi.stubGlobal('fetch', fetchMock);
    const previewUrlBuilder = () => '/api/workspace/entries/deck/content?preview=true';
    const original: FileInfo = {
      source: 'workspace',
      entry_id: 'deck',
      version_id: 'version-1',
      name: 'deck.pptx',
      path: 'deck.pptx',
      size: 4096,
      modified: '2026-08-28T10:00:00Z',
      revision: 1,
      type: 'pptx',
    };
    const renderPreview = (file: FileInfo) => (
      <FilePreview
        inline
        refreshInPlace
        file={file}
        sessionId="workspace:persistent"
        previewUrlBuilder={previewUrlBuilder}
        onClose={() => {}}
      />
    );
    const { rerender } = render(renderPreview(original));
    expect(screen.getByTestId('file-preview-loading')).toBeInTheDocument();

    rerender(renderPreview({
      ...original,
      version_id: 'version-2',
      revision: 2,
      modified: '2026-08-28T10:00:01Z',
    }));

    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(screen.getByTestId('file-preview-loading')).toBeInTheDocument();
    expect(screen.queryByText('当前环境无法生成在线预览，请下载原文件查看。')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '下载原文件' })).not.toBeInTheDocument();

    act(() => vi.advanceTimersByTime(OFFICE_PREVIEW_SLOW_HINT_MS));
    expect(screen.getByText('首次转换可能需要几十秒，你可以继续等待或下载原文件。')).toBeInTheDocument();
    expect(fetchMock.mock.calls[1][1]?.signal.aborted).toBe(false);
    expect(screen.getByTestId('file-preview-loading')).toBeInTheDocument();

    await act(async () => {
      secondResponse.resolve({
        ok: true,
        headers: { get: () => 'application/pdf' },
        blob: () => Promise.resolve(new Blob(['%PDF'], { type: 'application/pdf' })),
      });
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(screen.getByTestId('slide-deck-preview')).toHaveTextContent('deck.pptx');
    expect(screen.queryByTestId('file-preview-loading')).not.toBeInTheDocument();
    vi.useRealTimers();
  });

  it('renders PPTX through the server PDF conversion endpoint', async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      headers: { get: () => 'application/pdf' },
      blob: () => Promise.resolve(new Blob(['%PDF'], { type: 'application/pdf' })),
    });
    vi.stubGlobal('fetch', fetchMock);

    render(
      <FilePreview
        file={{ name: 'deck.pptx', path: 'deck.pptx', size: 4096, modified: '2026-08-26T01:10:00Z', type: 'pptx' }}
        sessionId="session-ppt"
        onClose={() => {}}
      />,
    );

    const viewer = await screen.findByTestId('slide-deck-preview');
    expect(viewer.getAttribute('data-source-key')).toContain('::render=pdf::');
    expect(viewer).toHaveTextContent('deck.pptx');
    expect(fetchMock).toHaveBeenCalledWith(
      '/api/sessions/session-ppt/files/deck.pptx?preview=true&render=pdf',
      expect.objectContaining({ signal: expect.any(AbortSignal) }),
    );
  });

  it('PPTX 服务端明确失败后才显示下载降级页', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: false, status: 504 }));

    render(
      <FilePreview
        file={{ name: 'slow.pptx', path: 'slow.pptx', size: 4096, modified: '2026-08-28T10:00:00Z', type: 'pptx' }}
        sessionId="session-ppt"
        onClose={() => {}}
      />,
    );

    expect(await screen.findByText('服务端暂时无法生成只读 PDF 预览，请下载原文件查看。')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '下载原文件' })).toBeInTheDocument();
  });

  it('DOCX 派生 PDF 在内嵌查看器 load 前保持稳定遮罩', async () => {
    mockObjectUrlApis('blob:docx-pdf');
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: true,
      headers: { get: () => 'application/pdf' },
      blob: () => Promise.resolve(new Blob(['%PDF'], { type: 'application/pdf' })),
    }));

    render(
      <FilePreview
        file={{ name: 'report.docx', path: 'report.docx', size: 2048, modified: '2026-08-26T01:15:00Z', type: 'docx' }}
        sessionId="session-docx"
        onClose={() => {}}
      />,
    );

    const iframe = await screen.findByTitle('report.docx PDF 预览');
    expect(iframe).toHaveClass('opacity-0');
    expect(screen.getByTestId('pdf-iframe-loading')).toBeInTheDocument();

    fireEvent.load(iframe);

    await waitFor(() => {
      expect(iframe).toHaveClass('opacity-100');
      expect(screen.queryByTestId('pdf-iframe-loading')).not.toBeInTheDocument();
    });
  });

  it('falls back to sanitized Mammoth HTML when DOCX PDF conversion fails', async () => {
    vi.mocked(mammoth.convertToHtml).mockResolvedValue({
      value: '<h1>安全正文</h1><script>window.evil=true</script>',
      messages: [],
    } as any);
    const fetchMock = vi.fn()
      .mockResolvedValueOnce({ ok: false, status: 422 })
      .mockResolvedValueOnce({ ok: true, arrayBuffer: () => Promise.resolve(new ArrayBuffer(8)) });
    vi.stubGlobal('fetch', fetchMock);

    const { container } = render(
      <FilePreview
        file={{ name: 'report.docx', path: 'report.docx', size: 2048, modified: '2026-08-26T01:20:00Z', type: 'docx' }}
        sessionId="session-docx"
        onClose={() => {}}
      />,
    );

    expect(await screen.findByText('安全正文')).toBeInTheDocument();
    expect(screen.getByText(/简化版式/)).toBeInTheDocument();
    expect(container.querySelector('script')).toBeNull();
    expect(fetchMock.mock.calls[0][0]).toBe('/api/sessions/session-docx/files/report.docx?preview=true&render=pdf');
    expect(fetchMock.mock.calls[1][0]).toBe('/api/sessions/session-docx/files/report.docx?preview=true');
  });

  it('parses quoted CSV cells, escaped quotes and embedded newlines', () => {
    expect(parseCsvRows('name,note\r\nAlpha,"one,two"\r\nBeta,"line 1\nline ""2"""')).toEqual([
      ['name', 'note'],
      ['Alpha', 'one,two'],
      ['Beta', 'line 1\nline "2"'],
    ]);
  });
});
