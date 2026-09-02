import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterAll, beforeEach, describe, expect, it, vi } from 'vitest';
import type { PDFDocumentProxy } from 'pdfjs-dist';
import { SlideDeckPreview } from '../../../components/file-preview/SlideDeckPreview';

const { getDocumentMock, workerOptions } = vi.hoisted(() => ({
  getDocumentMock: vi.fn(),
  workerOptions: { workerSrc: '' },
}));

vi.mock('pdfjs-dist', () => ({
  getDocument: getDocumentMock,
  GlobalWorkerOptions: workerOptions,
}));

vi.mock('pdfjs-dist/build/pdf.worker.min.mjs?url', () => ({
  default: '/assets/pdf.worker.test.mjs',
}));

interface ObservedIntersection {
  callback: IntersectionObserverCallback;
  options?: IntersectionObserverInit;
  targets: Set<Element>;
}

const intersectionObservers: ObservedIntersection[] = [];
const originalIntersectionObserver = window.IntersectionObserver;
const originalResizeObserver = window.ResizeObserver;
const originalDevicePixelRatio = Object.getOwnPropertyDescriptor(window, 'devicePixelRatio');

class IntersectionObserverMock implements IntersectionObserver {
  readonly root: Element | Document | null;
  readonly rootMargin: string;
  readonly thresholds: readonly number[];
  private readonly record: ObservedIntersection;

  constructor(callback: IntersectionObserverCallback, options?: IntersectionObserverInit) {
    this.root = options?.root || null;
    this.rootMargin = options?.rootMargin || '0px';
    this.thresholds = Array.isArray(options?.threshold)
      ? options.threshold
      : [options?.threshold ?? 0];
    this.record = { callback, options, targets: new Set() };
    intersectionObservers.push(this.record);
  }

  observe = (target: Element) => {
    this.record.targets.add(target);
  };

  unobserve = (target: Element) => {
    this.record.targets.delete(target);
  };

  disconnect = () => {
    this.record.targets.clear();
  };

  takeRecords = () => [];
}

class ResizeObserverMock implements ResizeObserver {
  private readonly callback: ResizeObserverCallback;

  constructor(callback: ResizeObserverCallback) {
    this.callback = callback;
  }

  observe = (target: Element) => {
    const width = target.classList.contains('slide-deck-preview__canvas-frame--thumbnail')
      ? 132
      : 900;
    this.callback([{
      target,
      contentRect: {
        width,
        height: width * 9 / 16,
      } as DOMRectReadOnly,
    } as ResizeObserverEntry], this);
  };

  unobserve = () => undefined;
  disconnect = () => undefined;
}

function triggerIntersection(target: Element, ratio = 1) {
  const observer = intersectionObservers.find((candidate) => candidate.targets.has(target));
  if (!observer) throw new Error('Target is not observed');
  observer.callback([{
    target,
    isIntersecting: ratio > 0,
    intersectionRatio: ratio,
  } as IntersectionObserverEntry], {} as IntersectionObserver);
}

function createPdfDocument(pageCount = 7) {
  const renderMock = vi.fn(() => ({
    promise: Promise.resolve(),
    cancel: vi.fn(),
  }));
  const getPageMock = vi.fn(async () => ({
    getViewport: ({ scale }: { scale: number }) => ({
      width: 1600 * scale,
      height: 900 * scale,
    }),
    render: renderMock,
  }));
  const document = {
    numPages: pageCount,
    getPage: getPageMock,
  } as unknown as PDFDocumentProxy;
  return { document, getPageMock, renderMock };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, reject, resolve };
}

beforeEach(() => {
  intersectionObservers.length = 0;
  getDocumentMock.mockReset();
  workerOptions.workerSrc = '';
  Object.defineProperty(window, 'IntersectionObserver', {
    configurable: true,
    value: IntersectionObserverMock,
  });
  Object.defineProperty(window, 'ResizeObserver', {
    configurable: true,
    value: ResizeObserverMock,
  });
  Object.defineProperty(window, 'devicePixelRatio', {
    configurable: true,
    value: 2,
  });
});

afterAll(() => {
  Object.defineProperty(window, 'IntersectionObserver', {
    configurable: true,
    value: originalIntersectionObserver,
  });
  Object.defineProperty(window, 'ResizeObserver', {
    configurable: true,
    value: originalResizeObserver,
  });
  if (originalDevicePixelRatio) {
    Object.defineProperty(window, 'devicePixelRatio', originalDevicePixelRatio);
  }
});

describe('SlideDeckPreview', () => {
  it('renders only the current page window at 2x DPR and navigates from thumbnails', async () => {
    const { document } = createPdfDocument();
    getDocumentMock.mockReturnValue({
      promise: Promise.resolve(document),
      destroy: vi.fn(() => Promise.resolve()),
    });

    const { container } = render(
      <SlideDeckPreview source="blob:converted-presentation" sourceKey="deck-1" requestId={1} activeRequestId={1} title="季度汇报.pptx" />,
    );

    await screen.findByLabelText('季度汇报.pptx 幻灯片预览');
    expect(screen.getByText('7 页')).toBeInTheDocument();

    await waitFor(() => {
      const rendered = Array.from(container.querySelectorAll<HTMLElement>('[data-slide-page]'))
        .filter((page) => page.dataset.rendered === 'true')
        .map((page) => page.dataset.pageNumber);
      expect(rendered).toEqual(['1', '2', '3']);
    });

    const firstMainCanvas = await waitFor(() => {
      const canvas = container.querySelector<HTMLCanvasElement>(
        'canvas[data-slide-canvas="page"][data-page-number="1"]',
      );
      expect(canvas).not.toBeNull();
      expect(canvas?.style.visibility).toBe('visible');
      return canvas as HTMLCanvasElement;
    });
    expect(firstMainCanvas.width).toBe(1800);
    expect(firstMainCanvas.style.width).toBe('900px');

    const pageOneButton = screen.getByRole('button', { name: '转到第 1 页' });
    expect(pageOneButton.querySelector('canvas')).toBeNull();
    act(() => triggerIntersection(pageOneButton));
    await waitFor(() => expect(pageOneButton.querySelector('canvas')).not.toBeNull());
    expect(screen.getByRole('button', { name: '转到第 2 页' }).querySelector('canvas')).toBeNull();

    fireEvent.click(screen.getByRole('button', { name: '转到第 6 页' }));
    expect(screen.getByRole('button', { name: '转到第 6 页' })).toHaveAttribute('aria-current', 'page');
    await waitFor(() => {
      const rendered = Array.from(container.querySelectorAll<HTMLElement>('[data-slide-page]'))
        .filter((page) => page.dataset.rendered === 'true')
        .map((page) => page.dataset.pageNumber);
      expect(rendered).toEqual(['4', '5', '6', '7']);
    });
  });

  it('syncs the active thumbnail when the continuous page viewport changes', async () => {
    const { document } = createPdfDocument(5);
    getDocumentMock.mockReturnValue({
      promise: Promise.resolve(document),
      destroy: vi.fn(() => Promise.resolve()),
    });
    const { container } = render(
      <SlideDeckPreview source="blob:scroll-sync" sourceKey="deck-scroll" requestId={1} activeRequestId={1} title="产业链分析.pptx" />,
    );

    await screen.findByLabelText('产业链分析.pptx 幻灯片预览');
    const pageFour = container.querySelector<HTMLElement>('[data-slide-page][data-page-number="4"]');
    expect(pageFour).not.toBeNull();
    act(() => triggerIntersection(pageFour as HTMLElement, 0.8));

    await waitFor(() => {
      expect(screen.getByRole('button', { name: '转到第 4 页' })).toHaveAttribute('aria-current', 'page');
    });
    const rendered = Array.from(container.querySelectorAll<HTMLElement>('[data-slide-page]'))
      .filter((page) => page.dataset.rendered === 'true')
      .map((page) => page.dataset.pageNumber);
    expect(rendered).toEqual(['2', '3', '4', '5']);
  });

  it('keeps the old deck visible until the replacement PDF and first page are ready', async () => {
    const { document: oldDocument } = createPdfDocument(2);
    const { document: newDocument } = createPdfDocument(5);
    const replacement = deferred<PDFDocumentProxy>();
    const destroyOld = vi.fn(() => Promise.resolve());
    const destroyNew = vi.fn(() => Promise.resolve());
    const onReady = vi.fn();
    getDocumentMock
      .mockReturnValueOnce({ promise: Promise.resolve(oldDocument), destroy: destroyOld })
      .mockReturnValueOnce({ promise: replacement.promise, destroy: destroyNew });

    const { rerender } = render(
      <SlideDeckPreview source="blob:old" sourceKey="deck-old" requestId={1} activeRequestId={1} title="原子切换.pptx" onReady={onReady} />,
    );
    expect(await screen.findByText('2 页')).toBeInTheDocument();

    rerender(
      <SlideDeckPreview source="blob:new" sourceKey="deck-new" requestId={2} activeRequestId={2} title="原子切换.pptx" onReady={onReady} />,
    );
    await waitFor(() => expect(getDocumentMock).toHaveBeenCalledTimes(2));
    expect(screen.getByText('2 页')).toBeInTheDocument();
    expect(screen.queryByText('正在准备幻灯片')).not.toBeInTheDocument();
    expect(destroyOld).not.toHaveBeenCalled();

    await act(async () => {
      replacement.resolve(newDocument);
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(await screen.findByText('5 页')).toBeInTheDocument();
    await waitFor(() => expect(destroyOld).toHaveBeenCalledTimes(1));
    expect(onReady).toHaveBeenLastCalledWith('deck-new');
  });

  it('keeps the active deck when a replacement fails', async () => {
    const { document: oldDocument } = createPdfDocument(3);
    const failure = new Error('replacement failed');
    const replacement = deferred<PDFDocumentProxy>();
    const destroyOld = vi.fn(() => Promise.resolve());
    const destroyReplacement = vi.fn(() => Promise.resolve());
    const onError = vi.fn();
    const consoleError = vi.spyOn(console, 'error').mockImplementation(() => undefined);
    getDocumentMock
      .mockReturnValueOnce({ promise: Promise.resolve(oldDocument), destroy: destroyOld })
      .mockReturnValueOnce({ promise: replacement.promise, destroy: destroyReplacement });

    const { rerender } = render(
      <SlideDeckPreview source="blob:old" sourceKey="deck-old" requestId={1} activeRequestId={1} title="刷新失败.pptx" />,
    );
    expect(await screen.findByText('3 页')).toBeInTheDocument();

    rerender(
      <SlideDeckPreview source="blob:bad" sourceKey="deck-bad" requestId={2} activeRequestId={2} title="刷新失败.pptx" onError={onError} />,
    );

    await waitFor(() => expect(getDocumentMock).toHaveBeenCalledTimes(2));
    await act(async () => {
      replacement.reject(failure);
      await Promise.resolve();
    });

    await waitFor(() => expect(onError).toHaveBeenCalledWith(failure, 'deck-bad'));
    expect(screen.getByText('3 页')).toBeInTheDocument();
    expect(screen.queryByTitle('刷新失败.pptx PDF 降级预览')).not.toBeInTheDocument();
    expect(destroyOld).not.toHaveBeenCalled();
    expect(destroyReplacement).toHaveBeenCalledTimes(1);
    consoleError.mockRestore();
  });

  it('discards a superseded replacement even when its promise resolves late', async () => {
    const { document: oldDocument } = createPdfDocument(2);
    const { document: lateDocument } = createPdfDocument(9);
    const { document: newestDocument } = createPdfDocument(4);
    const late = deferred<PDFDocumentProxy>();
    const destroyOld = vi.fn(() => Promise.resolve());
    const destroyLate = vi.fn(() => Promise.resolve());
    const destroyNewest = vi.fn(() => Promise.resolve());
    getDocumentMock
      .mockReturnValueOnce({ promise: Promise.resolve(oldDocument), destroy: destroyOld })
      .mockReturnValueOnce({ promise: late.promise, destroy: destroyLate })
      .mockReturnValueOnce({ promise: Promise.resolve(newestDocument), destroy: destroyNewest });

    const { rerender } = render(
      <SlideDeckPreview source="blob:old" sourceKey="deck-old" requestId={1} activeRequestId={1} title="竞态.pptx" />,
    );
    expect(await screen.findByText('2 页')).toBeInTheDocument();

    rerender(
      <SlideDeckPreview source="blob:late" sourceKey="deck-late" requestId={2} activeRequestId={2} title="竞态.pptx" />,
    );
    await waitFor(() => expect(getDocumentMock).toHaveBeenCalledTimes(2));
    rerender(
      <SlideDeckPreview source="blob:newest" sourceKey="deck-newest" requestId={3} activeRequestId={3} title="竞态.pptx" />,
    );

    expect(await screen.findByText('4 页')).toBeInTheDocument();
    await act(async () => {
      late.resolve(lateDocument);
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(screen.getByText('4 页')).toBeInTheDocument();
    expect(screen.queryByText('9 页')).not.toBeInTheDocument();
    expect(destroyLate).toHaveBeenCalledTimes(1);
  });

  it('falls back to the browser PDF viewer when PDF.js cannot load the deck', async () => {
    const failure = new Error('broken pdf');
    const onError = vi.fn();
    const consoleError = vi.spyOn(console, 'error').mockImplementation(() => undefined);
    getDocumentMock.mockReturnValue({
      promise: Promise.reject(failure),
      destroy: vi.fn(() => Promise.resolve()),
    });

    render(
      <SlideDeckPreview source="blob:broken-presentation" sourceKey="deck-broken" requestId={1} activeRequestId={1} title="失败示例.pptx" onError={onError} />,
    );

    expect(await screen.findByRole('alert')).toHaveTextContent('已切换为浏览器 PDF 预览');
    expect(screen.getByTitle('失败示例.pptx PDF 降级预览')).toHaveAttribute('src', 'blob:broken-presentation');
    expect(onError).toHaveBeenCalledWith(failure, 'deck-broken');
    consoleError.mockRestore();
  });

  it('destroys the PDF loading task when the viewer unmounts', async () => {
    const { document } = createPdfDocument(2);
    const destroyLoadingTask = vi.fn(() => Promise.resolve());
    getDocumentMock.mockReturnValue({
      promise: Promise.resolve(document),
      destroy: destroyLoadingTask,
    });
    const { unmount } = render(
      <SlideDeckPreview source="blob:cleanup" sourceKey="deck-cleanup" requestId={1} activeRequestId={1} title="清理测试.pptx" />,
    );

    await screen.findByLabelText('清理测试.pptx 幻灯片预览');
    unmount();
    expect(destroyLoadingTask).toHaveBeenCalledTimes(1);
  });
});
