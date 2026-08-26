import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { createRef, forwardRef, useEffect, useImperativeHandle, useRef, useState } from 'react';
import { describe, expect, it, vi, beforeEach, afterEach, afterAll } from 'vitest';
import JSZip from 'jszip';
import mammoth from 'mammoth';
import type { FileInfo } from '../../types';
import {
  FilePreview,
  type FilePreviewHandle,
  HTML_INLINE_PREVIEW_TIMEOUT_MS,
  MARKDOWN_AUTOSAVE_DELAY_MS,
  parseCsvRows,
  scheduleHtmlInlinePreviewTimeout,
} from '../../components/FilePreview';

const originalCreateObjectURL = Object.getOwnPropertyDescriptor(URL, 'createObjectURL');
const originalRevokeObjectURL = Object.getOwnPropertyDescriptor(URL, 'revokeObjectURL');
let markdownAdapterInitializationCount = 0;

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

const { getAuthHeadersMock, updateSessionMarkdownMock, updateSessionSpreadsheetMock } = vi.hoisted(() => ({
  getAuthHeadersMock: vi.fn(() => ({ Authorization: 'Bearer test-token' })),
  updateSessionMarkdownMock: vi.fn(),
  updateSessionSpreadsheetMock: vi.fn(),
}));

vi.mock('../../services/api', () => ({
  apiService: {
    getAuthHeaders: getAuthHeadersMock,
    downloadFile: vi.fn(),
    updateSessionMarkdown: updateSessionMarkdownMock,
    updateSessionSpreadsheet: updateSessionSpreadsheetMock,
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
  SlideDeckPreview: ({ src, title }: { src: string; title: string }) => (
    <div data-testid="slide-deck-preview" data-src={src}>{title}</div>
  ),
}));

vi.mock('../../components/file-preview/VditorMarkdownEditor', () => ({
  VditorMarkdownEditor: forwardRef(function MockVditorMarkdownEditor({
    markdown,
    onChange,
    filePath,
    buildSessionFileUrl,
  }: {
    markdown: string;
    onChange: (markdown: string) => void;
    filePath: string;
    buildSessionFileUrl: (path: string) => string;
  }, ref) {
    const valueRef = useRef(markdown);
    useEffect(() => {
      markdownAdapterInitializationCount += 1;
    }, [buildSessionFileUrl, filePath]);
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
  }),
}));

vi.mock('../../components/file-preview/SpreadsheetEditor', () => ({
  SpreadsheetEditor: forwardRef(function MockSpreadsheetEditor({
    fileName,
    readOnly,
    onMutation,
  }: {
    fileName: string;
    readOnly?: boolean;
    onMutation?: () => void;
  }, ref) {
    useImperativeHandle(ref, () => ({
      exportFile: () => new Uint8Array([0x50, 0x4b, 0x03, 0x04]).buffer,
    }), []);
    return (
      <div data-testid="spreadsheet-editor" data-readonly={String(Boolean(readOnly))}>
        <span>{fileName}</span>
        {!readOnly && <button type="button" onClick={onMutation}>模拟编辑单元格</button>}
      </div>
    );
  }),
}));

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
    vi.clearAllMocks();
    markdownAdapterInitializationCount = 0;
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

  it('Session Markdown 直接进入所见即所得编辑器', async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      text: () => Promise.resolve('![趋势图](./assets/chart.png)'),
    });
    vi.stubGlobal('fetch', fetchMock);

    render(
      <FilePreview
        file={{ ...markdownFile, path: 'reports/report.md' }}
        sessionId="session-markdown"
        onClose={() => {}}
      />,
    );

    expect(await screen.findByRole('textbox', { name: 'Markdown 所见即所得编辑器' })).toHaveValue(
      '![趋势图](./assets/chart.png)',
    );
    expect(screen.queryByRole('button', { name: '编辑 Markdown' })).not.toBeInTheDocument();
    const toolbarToggle = screen.getByRole('button', { name: '展开 Markdown 格式工具' });
    fireEvent.click(toolbarToggle);
    expect(toolbarToggle).toHaveAttribute('aria-expanded', 'true');
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

    const editor = await screen.findByRole('textbox', { name: 'Markdown 所见即所得编辑器' });
    fireEvent.change(editor, { target: { value: '# 已修改内容' } });
    expect(screen.getByText('等待自动保存')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '保存 Markdown' })).not.toBeInTheDocument();

    await waitFor(() => {
      expect(updateSessionMarkdownMock).toHaveBeenCalledWith(
        'session-markdown',
        expect.objectContaining({ size: 100, modified: '2026-04-16T18:30:00Z' }),
        '# 已修改内容',
      );
      expect(onFileUpdated).toHaveBeenCalledWith(expect.objectContaining({ size: 18 }));
      expect(screen.getByText('已自动保存')).toBeInTheDocument();
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
    const editor = await screen.findByRole('textbox', { name: 'Markdown 所见即所得编辑器' });
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
    const editor = await screen.findByRole('textbox', { name: 'Markdown 所见即所得编辑器' });
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

    const editor = await screen.findByRole('textbox', { name: 'Markdown 所见即所得编辑器' });
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
    );
    expect(await screen.findByText('已自动保存')).toBeInTheDocument();
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
    const editor = await screen.findByRole('textbox', { name: 'Markdown 所见即所得编辑器' });
    const initializationCountBeforeSave = markdownAdapterInitializationCount;
    editor.scrollTop = 160;
    fireEvent.change(editor, { target: { value: '# 已修改内容' } });

    await waitFor(() => expect(screen.getByText('已自动保存')).toBeInTheDocument(), {
      timeout: MARKDOWN_AUTOSAVE_DELAY_MS + 1800,
    });
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

    expect(await screen.findByRole('textbox', { name: 'Markdown 所见即所得编辑器' }))
      .toHaveValue('# hello preview');

    fireEvent.click(screen.getByTitle('下载文件'));

    await waitFor(() => {
      expect(screen.getByText('下载文件失败：文件不存在或尚未生成')).toBeInTheDocument();
    });
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
    expect(screen.getByRole('status')).toHaveTextContent('等待自动保存');
    await waitFor(() => {
      expect(updateSessionSpreadsheetMock).toHaveBeenCalledWith(
        'session-3',
        expect.objectContaining({ size: 2048, modified: '2026-04-22T10:20:00Z' }),
        expect.any(ArrayBuffer),
      );
      expect(onFileUpdated).toHaveBeenCalledWith(expect.objectContaining({ size: 4096 }));
      expect(screen.getByRole('status')).toHaveTextContent('已自动保存');
    }, { timeout: 2500 });
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

  it('renders PPTX through the server PDF conversion endpoint', async () => {
    const { createObjectURLMock } = mockObjectUrlApis('blob:ppt-pdf');
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
    expect(viewer).toHaveAttribute('data-src', 'blob:ppt-pdf');
    expect(viewer).toHaveTextContent('deck.pptx');
    expect(fetchMock).toHaveBeenCalledWith(
      '/api/sessions/session-ppt/files/deck.pptx?preview=true&render=pdf',
      expect.objectContaining({ signal: expect.any(AbortSignal) }),
    );
    expect(createObjectURLMock).toHaveBeenCalledTimes(1);
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
