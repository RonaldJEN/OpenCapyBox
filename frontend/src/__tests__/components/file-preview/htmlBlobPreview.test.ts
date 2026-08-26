import { afterAll, afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import {
  HTML_BLOB_PREVIEW_TTL_MS,
  openBlobPreviewInWindow,
  openHtmlBlobPreview,
  revokeRetainedHtmlPreviewUrls,
} from '../../../components/file-preview/htmlBlobPreview';

const originalCreateObjectURL = Object.getOwnPropertyDescriptor(URL, 'createObjectURL');
const originalRevokeObjectURL = Object.getOwnPropertyDescriptor(URL, 'revokeObjectURL');

function readBlobText(blob: Blob): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result || ''));
    reader.onerror = () => reject(reader.error);
    reader.readAsText(blob);
  });
}

describe('HTML Blob 单独查看', () => {
  let createObjectURL: ReturnType<typeof vi.fn>;
  let revokeObjectURL: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    vi.useFakeTimers();
    createObjectURL = vi.fn()
      .mockReturnValueOnce('blob:content')
      .mockReturnValueOnce('blob:wrapper');
    revokeObjectURL = vi.fn();
    Object.defineProperty(URL, 'createObjectURL', {
      configurable: true,
      value: createObjectURL,
    });
    Object.defineProperty(URL, 'revokeObjectURL', {
      configurable: true,
      value: revokeObjectURL,
    });
  });

  afterEach(() => {
    revokeRetainedHtmlPreviewUrls();
    vi.useRealTimers();
  });

  afterAll(() => {
    if (originalCreateObjectURL) Object.defineProperty(URL, 'createObjectURL', originalCreateObjectURL);
    else delete (URL as { createObjectURL?: unknown }).createObjectURL;
    if (originalRevokeObjectURL) Object.defineProperty(URL, 'revokeObjectURL', originalRevokeObjectURL);
    else delete (URL as { revokeObjectURL?: unknown }).revokeObjectURL;
  });

  it('window.open 返回 null 时立即回收两个 URL 并抛出可提示错误', () => {
    expect(() => openHtmlBlobPreview('<h1>demo</h1>', 'demo', () => null))
      .toThrow('浏览器阻止了新标签页');
    expect(revokeObjectURL).toHaveBeenCalledWith('blob:content');
    expect(revokeObjectURL).toHaveBeenCalledWith('blob:wrapper');
  });

  it('成功打开后断开 opener 并在有界 TTL 到期时回收', () => {
    const openedWindow = { opener: window } as unknown as Window;
    const openWindow = vi.fn(() => openedWindow);

    openHtmlBlobPreview('<h1>demo</h1>', 'demo', openWindow);

    expect(openWindow).toHaveBeenCalledWith('blob:wrapper', '_blank');
    expect(openedWindow.opener).toBeNull();
    expect(revokeObjectURL).not.toHaveBeenCalled();

    vi.advanceTimersByTime(HTML_BLOB_PREVIEW_TTL_MS);
    expect(revokeObjectURL).toHaveBeenCalledWith('blob:content');
    expect(revokeObjectURL).toHaveBeenCalledWith('blob:wrapper');
  });

  it('pagehide 时回收仍被保留的 Blob URL', () => {
    const openedWindow = { opener: window } as unknown as Window;
    openHtmlBlobPreview('<h1>demo</h1>', 'demo', () => openedWindow);

    window.dispatchEvent(new Event('pagehide'));

    expect(revokeObjectURL).toHaveBeenCalledWith('blob:content');
    expect(revokeObjectURL).toHaveBeenCalledWith('blob:wrapper');
  });

  it.each([
    {
      kind: 'HTML',
      type: 'text/html',
      payload: '<script>window.parent.localStorage.getItem("accessToken")</script>',
    },
    {
      kind: 'SVG',
      type: 'image/svg+xml',
      payload: '<svg xmlns="http://www.w3.org/2000/svg" onload="localStorage.getItem(\'accessToken\')"/>',
    },
  ])('鉴权取得的恶意 $kind 只能进入无同源权限的 sandbox iframe', async ({ type, payload }) => {
    vi.useRealTimers();
    const content = new Blob([payload], { type });
    const replace = vi.fn();
    const openedWindow = {
      opener: window,
      location: { replace },
    } as unknown as Window;

    openBlobPreviewInWindow(content, '会话文件', openedWindow, 60_000);

    expect(createObjectURL).toHaveBeenNthCalledWith(1, content);
    expect(replace).toHaveBeenCalledWith('blob:wrapper');
    expect(replace).not.toHaveBeenCalledWith('blob:content');
    expect(openedWindow.opener).toBeNull();

    const wrapperBlob = createObjectURL.mock.calls[1][0] as Blob;
    const wrapper = await readBlobText(wrapperBlob);
    expect(wrapper).toContain('src="blob:content"');
    expect(wrapper).toContain('sandbox="allow-scripts"');
    expect(wrapper).not.toContain('allow-same-origin');
    expect(wrapper).not.toContain('accessToken');
  });

  it('预先打开的窗口按调用方 TTL 回收内容与包装页 URL', () => {
    const content = new Blob(['pdf'], { type: 'application/pdf' });
    const openedWindow = {
      opener: window,
      location: { replace: vi.fn() },
    } as unknown as Window;

    openBlobPreviewInWindow(content, '会话文件', openedWindow, 60_000);

    vi.advanceTimersByTime(59_999);
    expect(revokeObjectURL).not.toHaveBeenCalled();
    vi.advanceTimersByTime(1);
    expect(revokeObjectURL).toHaveBeenCalledWith('blob:content');
    expect(revokeObjectURL).toHaveBeenCalledWith('blob:wrapper');
  });
});
