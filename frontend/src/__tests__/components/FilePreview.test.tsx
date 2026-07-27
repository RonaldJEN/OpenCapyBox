import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { describe, expect, it, vi, beforeEach, afterEach, afterAll } from 'vitest';
import * as XLSX from 'xlsx';
import JSZip from 'jszip';
import type { FileInfo } from '../../types';
import { FilePreview } from '../../components/FilePreview';

const originalCreateObjectURL = Object.getOwnPropertyDescriptor(URL, 'createObjectURL');
const originalRevokeObjectURL = Object.getOwnPropertyDescriptor(URL, 'revokeObjectURL');

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

const { getAuthHeadersMock } = vi.hoisted(() => ({
  getAuthHeadersMock: vi.fn(() => ({ Authorization: 'Bearer test-token' })),
}));

vi.mock('../../services/api', () => ({
  apiService: {
    getAuthHeaders: getAuthHeadersMock,
    downloadFile: vi.fn(),
  },
}));

vi.mock('mammoth', () => ({
  default: {
    convertToHtml: vi.fn(),
  },
}));

vi.mock('xlsx', () => ({
  read: vi.fn(),
  utils: {
    sheet_to_json: vi.fn(),
  },
}));

vi.mock('react-syntax-highlighter', () => ({
  Prism: ({ children }: { children: string }) => <pre>{children}</pre>,
}));

vi.mock('react-syntax-highlighter/dist/esm/styles/prism', () => ({
  vscDarkPlus: {},
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
    mockObjectUrlApis('blob:default');
  });

  afterEach(() => {
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
      expect(fetchMock).toHaveBeenCalledWith('/api/cron/runs/run-1/files/report.md?preview=true', {
        headers: { Authorization: 'Bearer test-token' },
      });
    });

    expect(await screen.findByText('hello preview')).toBeInTheDocument();

    fireEvent.click(screen.getByTitle('下载文件'));
    await waitFor(() => {
      expect(onDownloadFile).toHaveBeenCalledWith(markdownFile);
    });
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

    expect(await screen.findByText('hello preview')).toBeInTheDocument();

    fireEvent.click(screen.getByTitle('下载文件'));

    await waitFor(() => {
      expect(screen.getByText('下载文件失败：文件不存在或尚未生成')).toBeInTheDocument();
    });
  });

  it('renders HTML with blob iframe sandbox', async () => {
    const htmlFile: FileInfo = {
      name: 'preview.html',
      path: 'preview.html',
      size: 128,
      modified: '2026-04-22T10:00:00Z',
      type: 'html',
      is_directory: false,
    };

    const { createObjectURLMock } = mockObjectUrlApis('blob:html-preview');
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      text: () => Promise.resolve('<!doctype html><html><body><h1>HTML Preview</h1></body></html>'),
    });
    vi.stubGlobal('fetch', fetchMock);

    render(
      <FilePreview
        file={htmlFile}
        sessionId="session-1"
        onClose={() => {}}
      />,
    );

    await waitFor(() => {
      expect(createObjectURLMock).toHaveBeenCalledTimes(1);
    });

    const iframe = await screen.findByTitle('preview.html');
    expect(iframe).toHaveAttribute('src', 'blob:html-preview');
    expect(iframe).toHaveAttribute('sandbox', 'allow-scripts');
    expect(iframe).not.toHaveAttribute('srcdoc');

    expect(screen.getByTestId('html-iframe-loading')).toBeInTheDocument();
    fireEvent.load(iframe);
    await waitFor(() => {
      expect(screen.queryByTestId('html-iframe-loading')).not.toBeInTheDocument();
    });
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
      expect(fetchMock).toHaveBeenCalledWith('/api/sessions/session-1/files/%E4%B8%AD%E6%96%87%20%E5%9B%BE.png?preview=true', {
        headers: { Authorization: 'Bearer test-token' },
      });
      expect(createObjectURLMock).toHaveBeenCalledTimes(1);
    });

    expect(screen.getByAltText('中文 图.png')).toHaveAttribute('src', 'blob:image-preview');
  });

  it('revokes HTML blob URLs when switching files and unmounting', async () => {
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

    await waitFor(() => {
      expect(createObjectURLMock).toHaveBeenCalledTimes(1);
    });

    rerender(
      <FilePreview
        file={htmlFileB}
        sessionId="session-2"
        onClose={() => {}}
      />,
    );

    await waitFor(() => {
      expect(createObjectURLMock).toHaveBeenCalledTimes(2);
    });
    expect(revokeObjectURLMock).toHaveBeenCalledWith('blob:first');

    unmount();
    expect(revokeObjectURLMock).toHaveBeenCalledWith('blob:second');
  });

  it('renders XLSX workbook sheets as table previews', async () => {
    const xlsxFile: FileInfo = {
      name: 'metrics.xlsx',
      path: 'metrics.xlsx',
      size: 2048,
      modified: '2026-04-22T10:20:00Z',
      type: 'xlsx',
      is_directory: false,
    };
    const summarySheet = {};
    const rawSheet = {};
    vi.mocked(XLSX.read).mockReturnValue({
      SheetNames: ['Summary', 'Raw'],
      Sheets: {
        Summary: summarySheet,
        Raw: rawSheet,
      },
    } as any);
    vi.mocked(XLSX.utils.sheet_to_json)
      .mockReturnValueOnce([
        ['Metric', 'Value'],
        ['Rows', 42],
      ] as any)
      .mockReturnValueOnce([
        ['ID', 'Name'],
        [1, 'Alpha'],
      ] as any);
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      arrayBuffer: () => Promise.resolve(new ArrayBuffer(8)),
    });
    vi.stubGlobal('fetch', fetchMock);

    render(
      <FilePreview
        file={xlsxFile}
        sessionId="session-3"
        onClose={() => {}}
      />,
    );

    await waitFor(() => {
      expect(XLSX.read).toHaveBeenCalledWith(expect.any(ArrayBuffer), { type: 'array', cellDates: true });
    });
    expect(screen.queryByText('出于安全原因，已禁用 XLS/XLSX 在线预览')).not.toBeInTheDocument();
    expect(screen.getByText('Metric')).toBeInTheDocument();
    expect(screen.getByText('Rows')).toBeInTheDocument();
    expect(screen.getByText('42')).toBeInTheDocument();

    fireEvent.click(screen.getByRole('tab', { name: 'Raw' }));

    expect(screen.getByText('ID')).toBeInTheDocument();
    expect(screen.getByText('Alpha')).toBeInTheDocument();
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
});
