import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterAll, afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import {
  MarkdownReportPreview,
  resolveMarkdownSessionPath,
} from '../../../components/file-preview/MarkdownReportPreview';
import { revokeRetainedHtmlPreviewUrls } from '../../../components/file-preview/htmlBlobPreview';

const originalCreateObjectURL = Object.getOwnPropertyDescriptor(URL, 'createObjectURL');
const originalRevokeObjectURL = Object.getOwnPropertyDescriptor(URL, 'revokeObjectURL');

const { getAuthHeaders } = vi.hoisted(() => ({
  getAuthHeaders: vi.fn(() => ({ Authorization: 'Bearer markdown-test' })),
}));

function readBlobText(blob: Blob): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result || ''));
    reader.onerror = () => reject(reader.error);
    reader.readAsText(blob);
  });
}

vi.mock('../../../services/api', () => ({
  apiService: { getAuthHeaders },
}));

describe('Markdown 平面阅读工作区', () => {
  const report = [
    '# 核心结论',
    '正文。',
    '## 持仓结构',
    '结构说明。',
    '## 风险提示',
    '风险说明。',
  ].join('\n\n');

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('目录默认关闭，工具栏按钮公开动态状态与提示', () => {
    render(<MarkdownReportPreview content={report} />);

    const toolbar = screen.getByRole('toolbar', { name: 'Markdown 阅读工具' });
    const toggle = within(toolbar).getByRole('button', { name: '展开目录' });
    expect(toggle).toHaveAttribute('title', '展开目录');
    expect(toggle).toHaveAttribute('aria-expanded', 'false');
    expect(screen.queryByRole('navigation', { name: '文档目录' })).not.toBeInTheDocument();
    expect(document.querySelector('.file-preview-report-layout')).toHaveAttribute('data-toc-open', 'false');
    expect(document.querySelector('.file-preview-report-viewport')).toContainElement(
      screen.getByRole('article'),
    );
  });

  it('展开后渲染独立目录栏，点击锚点更新当前位置并可收起', () => {
    render(<MarkdownReportPreview content={report} />);

    const toolbar = screen.getByRole('toolbar', { name: 'Markdown 阅读工具' });
    fireEvent.click(within(toolbar).getByRole('button', { name: '展开目录' }));

    const toggle = within(toolbar).getByRole('button', { name: '收起目录' });
    expect(toggle).toHaveAttribute('title', '收起目录');
    expect(toggle).toHaveAttribute('aria-expanded', 'true');
    const toc = screen.getByRole('navigation', { name: '文档目录' });
    const firstLink = within(toc).getByRole('link', { name: '核心结论' });
    const riskLink = within(toc).getByRole('link', { name: '风险提示' });
    expect(firstLink).toHaveAttribute('href', '#核心结论');
    expect(firstLink).toHaveAttribute('aria-current', 'location');

    fireEvent.click(riskLink);
    expect(riskLink).toHaveAttribute('aria-current', 'location');
    expect(firstLink).not.toHaveAttribute('aria-current');

    fireEvent.click(toggle);
    expect(screen.queryByRole('navigation', { name: '文档目录' })).not.toBeInTheDocument();
    expect(within(toolbar).getByRole('button', { name: '展开目录' })).toHaveAttribute('aria-expanded', 'false');
  });

  it('正文滚动观察结果会同步目录当前位置', async () => {
    let observerCallback: IntersectionObserverCallback | null = null;
    class IntersectionObserverMock implements IntersectionObserver {
      readonly root = null;
      readonly rootMargin = '';
      readonly thresholds = [0];
      constructor(callback: IntersectionObserverCallback) {
        observerCallback = callback;
      }
      observe = vi.fn();
      unobserve = vi.fn();
      disconnect = vi.fn();
      takeRecords = vi.fn(() => []);
    }
    vi.stubGlobal('IntersectionObserver', IntersectionObserverMock);

    render(<MarkdownReportPreview content={report} />);
    const toolbar = screen.getByRole('toolbar', { name: 'Markdown 阅读工具' });
    fireEvent.click(within(toolbar).getByRole('button', { name: '展开目录' }));
    const toc = screen.getByRole('navigation', { name: '文档目录' });
    const riskHeading = screen.getByRole('heading', { name: '风险提示' });

    await waitFor(() => expect(observerCallback).not.toBeNull());
    act(() => {
      const callback = observerCallback as IntersectionObserverCallback;
      callback([{
        target: riskHeading,
        isIntersecting: true,
        boundingClientRect: { top: 12 } as DOMRectReadOnly,
      } as unknown as IntersectionObserverEntry], {} as IntersectionObserver);
    });

    expect(within(toc).getByRole('link', { name: '风险提示' }))
      .toHaveAttribute('aria-current', 'location');
  });

  it('仅有一个章节时不展示无效目录按钮', () => {
    render(<MarkdownReportPreview content="# 唯一章节\n\n正文。" />);

    expect(screen.queryByRole('button', { name: '展开目录' })).not.toBeInTheDocument();
    expect(screen.getByText('1 个章节')).toBeInTheDocument();
  });
});

describe('Markdown 会话资源解析', () => {
  const buildSessionFileUrl = vi.fn((path: string) => (
    `/api/sessions/s1/files/${path.split('/').map(encodeURIComponent).join('/')}?preview=true`
  ));

  beforeEach(() => {
    vi.clearAllMocks();
    Object.defineProperty(URL, 'createObjectURL', {
      configurable: true,
      value: vi.fn(() => 'blob:markdown-resource'),
    });
    Object.defineProperty(URL, 'revokeObjectURL', {
      configurable: true,
      value: vi.fn(),
    });
  });

  afterEach(() => {
    revokeRetainedHtmlPreviewUrls();
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  afterAll(() => {
    if (originalCreateObjectURL) Object.defineProperty(URL, 'createObjectURL', originalCreateObjectURL);
    else delete (URL as { createObjectURL?: unknown }).createObjectURL;
    if (originalRevokeObjectURL) Object.defineProperty(URL, 'revokeObjectURL', originalRevokeObjectURL);
    else delete (URL as { revokeObjectURL?: unknown }).revokeObjectURL;
  });

  it('允许目录内相对寻址并拒绝越过 session 根目录', () => {
    expect(resolveMarkdownSessionPath('reports/2026/report.md', '../assets/chart.png'))
      .toBe('reports/assets/chart.png');
    expect(resolveMarkdownSessionPath('reports/2026/report.md', '/shared/source.pdf'))
      .toBe('shared/source.pdf');
    expect(resolveMarkdownSessionPath('reports/report.md', '../../../secret.txt')).toBeNull();
    expect(resolveMarkdownSessionPath('report.md', '%2e%2e/secret.txt')).toBeNull();
    expect(resolveMarkdownSessionPath('report.md', 'https://example.com/a.png')).toBeNull();
  });

  it('相对图片通过带 Authorization 的会话文件接口加载', async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(new Blob(['image'], { type: 'image/png' }), { status: 200 }),
    );
    vi.stubGlobal('fetch', fetchMock);

    render(
      <MarkdownReportPreview
        content="![资产图](../assets/chart.png)"
        filePath="reports/2026/report.md"
        buildSessionFileUrl={buildSessionFileUrl}
      />,
    );

    await waitFor(() => expect(screen.getByAltText('资产图')).toHaveAttribute('src', 'blob:markdown-resource'));
    expect(fetchMock).toHaveBeenCalledWith(
      '/api/sessions/s1/files/reports/assets/chart.png?preview=true',
      expect.objectContaining({ headers: { Authorization: 'Bearer markdown-test' } }),
    );
  });

  it('相对链接经鉴权 fetch 后在 sandbox Blob 包装页打开', async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(new Blob(['pdf'], { type: 'application/pdf' }), { status: 200 }),
    );
    vi.stubGlobal('fetch', fetchMock);
    vi.mocked(URL.createObjectURL)
      .mockReset()
      .mockReturnValueOnce('blob:markdown-content')
      .mockReturnValueOnce('blob:markdown-wrapper');
    const replace = vi.fn();
    const close = vi.fn();
    const openedWindow = {
      opener: window,
      location: { replace },
      close,
    } as unknown as Window;
    const openMock = vi.spyOn(window, 'open').mockReturnValue(openedWindow);

    render(
      <MarkdownReportPreview
        content="[查看附录](./appendix.pdf)"
        filePath="reports/report.md"
        buildSessionFileUrl={buildSessionFileUrl}
      />,
    );
    fireEvent.click(screen.getByRole('link', { name: '查看附录' }));

    await waitFor(() => expect(replace).toHaveBeenCalledWith('blob:markdown-wrapper'));
    expect(replace).not.toHaveBeenCalledWith('blob:markdown-content');
    expect(openMock).toHaveBeenCalledWith('about:blank', '_blank');
    expect(openedWindow.opener).toBeNull();
    expect(fetchMock).toHaveBeenCalledWith(
      '/api/sessions/s1/files/reports/appendix.pdf?preview=true',
      { headers: { Authorization: 'Bearer markdown-test' } },
    );
    expect(close).not.toHaveBeenCalled();
  });

  it.each([
    {
      kind: 'HTML',
      path: './malicious.html',
      type: 'text/html',
      payload: '<script>localStorage.getItem("accessToken")</script>',
    },
    {
      kind: 'SVG',
      path: './malicious.svg',
      type: 'image/svg+xml',
      payload: '<svg xmlns="http://www.w3.org/2000/svg" onload="localStorage.getItem(\'accessToken\')"/>',
    },
  ])('相对恶意 $kind 不会被导航为顶层同源 Blob', async ({ path, type, payload }) => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(new Blob([payload], { type }), { status: 200 }),
    );
    vi.stubGlobal('fetch', fetchMock);
    const createdBlobs: Blob[] = [];
    vi.mocked(URL.createObjectURL).mockReset().mockImplementation((value) => {
      createdBlobs.push(value as Blob);
      return createdBlobs.length === 1 ? 'blob:malicious-content' : 'blob:trusted-wrapper';
    });
    const replace = vi.fn();
    const openedWindow = {
      opener: window,
      location: { replace },
      close: vi.fn(),
    } as unknown as Window;
    vi.spyOn(window, 'open').mockReturnValue(openedWindow);

    render(
      <MarkdownReportPreview
        content={`[打开${type}](${path})`}
        filePath="reports/report.md"
        buildSessionFileUrl={buildSessionFileUrl}
      />,
    );
    fireEvent.click(screen.getByRole('link', { name: `打开${type}` }));

    await waitFor(() => expect(replace).toHaveBeenCalledWith('blob:trusted-wrapper'));
    expect(replace).not.toHaveBeenCalledWith('blob:malicious-content');
    expect(createdBlobs).toHaveLength(2);
    const wrapper = await readBlobText(createdBlobs[1]);
    expect(wrapper).toContain('src="blob:malicious-content"');
    expect(wrapper).toContain('sandbox="allow-scripts"');
    expect(wrapper).not.toContain('allow-same-origin');
    expect(wrapper).not.toContain('accessToken');
  });

  it('弹窗被阻止时不发起鉴权请求并保留可见错误', async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal('fetch', fetchMock);
    vi.spyOn(window, 'open').mockReturnValue(null);

    render(
      <MarkdownReportPreview
        content="[查看附录](./appendix.pdf)"
        filePath="reports/report.md"
        buildSessionFileUrl={buildSessionFileUrl}
      />,
    );
    fireEvent.click(screen.getByRole('link', { name: '查看附录' }));

    expect(await screen.findByRole('status')).toHaveTextContent('浏览器阻止了新标签页');
    expect(fetchMock).not.toHaveBeenCalled();
    expect(URL.createObjectURL).not.toHaveBeenCalled();
  });

  it('越界相对链接和图片不生成可请求 URL', () => {
    render(
      <MarkdownReportPreview
        content={'[越界文件](../../secret.txt)\n\n![越界图片](../../secret.png)'}
        filePath="reports/report.md"
        buildSessionFileUrl={buildSessionFileUrl}
      />,
    );

    expect(screen.queryByRole('link', { name: '越界文件' })).not.toBeInTheDocument();
    expect(screen.getByText('越界文件')).toHaveAttribute('aria-disabled', 'true');
    expect(screen.getByLabelText('越界图片路径无效')).toBeInTheDocument();
    expect(buildSessionFileUrl).not.toHaveBeenCalled();
  });
});
