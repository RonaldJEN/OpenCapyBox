import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest';
import type { FileInfo } from '../../types';
import { FilePreview } from '../../components/FilePreview';

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

vi.mock('react-syntax-highlighter', () => ({
  Prism: ({ children }: { children: string }) => <pre>{children}</pre>,
}));

vi.mock('react-syntax-highlighter/dist/esm/styles/prism', () => ({
  vscDarkPlus: {},
}));

describe('FilePreview custom source', () => {
  const file: FileInfo = {
    name: 'report.md',
    path: 'report.md',
    size: 100,
    modified: '2026-04-16T18:30:00Z',
    type: 'md',
    is_directory: false,
  };

  beforeEach(() => {
    vi.clearAllMocks();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
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
        file={file}
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
      expect(onDownloadFile).toHaveBeenCalledWith(file);
    });
  });
});
