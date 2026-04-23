import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '../utils/test-utils';
import { ArtifactsPanel } from '../../components/ArtifactsPanel';
import { apiService } from '../../services/api';
import { FileInfo } from '../../types';

vi.mock('../../components/FilePreview', () => ({
  FilePreview: ({ file, inline, onClose }: any) => (
    <div data-testid="file-preview-inline-mock" data-inline={String(inline)}>
      <span>Inline Preview: {file?.name}</span>
      <button onClick={onClose}>Close Inline Preview</button>
    </div>
  ),
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

  it('点击关闭按钮应该调用 onClose', () => {
    const mockOnClose = vi.fn();

    render(
      <ArtifactsPanel
        sessionId="test-session"
        isOpen
        onClose={mockOnClose}
      />,
    );

    fireEvent.click(screen.getByRole('button', { name: '关闭面板' }));

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
