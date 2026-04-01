import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '../utils/test-utils';
import { ArtifactsPanel } from '../../components/ArtifactsPanel';
import { apiService } from '../../services/api';
import { FileInfo } from '../../types';

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
      size: 1024 * 100, // 100KB
      type: 'application/pdf',
      modified: new Date().toISOString(),
    },
    {
      name: 'data.xlsx',
      path: '/workspace/data.xlsx',
      size: 1024 * 50, // 50KB
      type: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
      modified: new Date().toISOString(),
    },
    {
      name: 'script.py',
      path: '/workspace/script.py',
      size: 1024 * 5, // 5KB
      type: 'text/x-python',
      modified: new Date().toISOString(),
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
        onFilePreview={vi.fn()}
      />
    );

    expect(apiService.getSessionFiles).not.toHaveBeenCalled();
  });

  it('面板打开时应该加载文件列表', async () => {
    render(
      <ArtifactsPanel
        sessionId="test-session"
        isOpen={true}
        onClose={vi.fn()}
        onFilePreview={vi.fn()}
      />
    );

    await waitFor(() => {
      expect(apiService.getSessionFiles).toHaveBeenCalledWith('test-session');
    });
  });

  it('应该显示文件列表', async () => {
    render(
      <ArtifactsPanel
        sessionId="test-session"
        isOpen={true}
        onClose={vi.fn()}
        onFilePreview={vi.fn()}
      />
    );

    await waitFor(() => {
      expect(screen.getByText('report.pdf')).toBeInTheDocument();
      expect(screen.getByText('data.xlsx')).toBeInTheDocument();
      expect(screen.getByText('script.py')).toBeInTheDocument();
    });
  });

  it('应该显示面板标题', async () => {
    render(
      <ArtifactsPanel
        sessionId="test-session"
        isOpen={true}
        onClose={vi.fn()}
        onFilePreview={vi.fn()}
      />
    );

    expect(screen.getByText('会话资源管理')).toBeInTheDocument();
  });

  it('点击关闭按钮应该调用 onClose', async () => {
    const mockOnClose = vi.fn();

    render(
      <ArtifactsPanel
        sessionId="test-session"
        isOpen={true}
        onClose={mockOnClose}
        onFilePreview={vi.fn()}
      />
    );

    // 找到关闭按钮并点击
    const closeButton = screen.getByRole('button', { name: '' }); // X 按钮没有文字
    fireEvent.click(closeButton);

    expect(mockOnClose).toHaveBeenCalled();
  });

  it('点击文件应该调用 onFilePreview', async () => {
    const mockOnFilePreview = vi.fn();

    render(
      <ArtifactsPanel
        sessionId="test-session"
        isOpen={true}
        onClose={vi.fn()}
        onFilePreview={mockOnFilePreview}
      />
    );

    await waitFor(() => {
      expect(screen.getByText('report.pdf')).toBeInTheDocument();
    });

    fireEvent.click(screen.getByText('report.pdf'));

    expect(mockOnFilePreview).toHaveBeenCalledWith(mockFiles[0]);
  });

  it('空文件列表应该显示提示信息', async () => {
    vi.mocked(apiService.getSessionFiles).mockResolvedValue({
      files: [],
      total: 0,
    });

    render(
      <ArtifactsPanel
        sessionId="test-session"
        isOpen={true}
        onClose={vi.fn()}
        onFilePreview={vi.fn()}
      />
    );

    await waitFor(() => {
      expect(screen.getByText('暂无文件')).toBeInTheDocument();
    });
  });

  it('加载中应该显示加载动画', () => {
    vi.mocked(apiService.getSessionFiles).mockImplementation(
      () => new Promise(() => {}) // 永不 resolve
    );

    render(
      <ArtifactsPanel
        sessionId="test-session"
        isOpen={true}
        onClose={vi.fn()}
        onFilePreview={vi.fn()}
      />
    );

    // 检查是否有加载动画
    expect(document.querySelector('.animate-spin')).toBeInTheDocument();
  });

  it('应该显示文件大小', async () => {
    render(
      <ArtifactsPanel
        sessionId="test-session"
        isOpen={true}
        onClose={vi.fn()}
        onFilePreview={vi.fn()}
      />
    );

    await waitFor(() => {
      // 100KB 文件
      expect(screen.getByText(/100.*KB/)).toBeInTheDocument();
    });
  });

  it('面板应该有滑入动画类', () => {
    const { container } = render(
      <ArtifactsPanel
        sessionId="test-session"
        isOpen={true}
        onClose={vi.fn()}
        onFilePreview={vi.fn()}
      />
    );

    // 检查面板是否有 translate-x-0 类（打开状态）
    const panel = container.firstChild as HTMLElement;
    expect(panel.className).toContain('translate-x-0');
  });

  it('面板关闭时应该有滑出动画类', () => {
    const { container } = render(
      <ArtifactsPanel
        sessionId="test-session"
        isOpen={false}
        onClose={vi.fn()}
        onFilePreview={vi.fn()}
      />
    );

    // 检查面板是否有 translate-x-full 类（关闭状态）
    const panel = container.firstChild as HTMLElement;
    expect(panel.className).toContain('translate-x-full');
  });
});
