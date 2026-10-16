import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { DocumentPdfPreview } from '../../../components/file-preview/DocumentPdfPreview';

const pdf = vi.hoisted(() => {
  type Listener = (payload: unknown) => void;

  const state = {
    getDocument: vi.fn(),
    eventBuses: [] as Array<{ dispatched: Array<[string, Record<string, unknown>]>; emit: (name: string, payload: unknown) => void }>,
    viewers: [] as Array<Record<string, any>>,
    linkServices: [] as Array<Record<string, any>>,
    resizeObservers: [] as Array<{ trigger: () => void; disconnect: ReturnType<typeof vi.fn> }>,
  };

  class EventBus {
    readonly listeners = new Map<string, Listener[]>();
    readonly dispatched: Array<[string, Record<string, unknown>]> = [];

    constructor() {
      state.eventBuses.push(this);
    }

    on(name: string, listener: Listener) {
      this.listeners.set(name, [...(this.listeners.get(name) || []), listener]);
    }

    dispatch(name: string, payload: Record<string, unknown>) {
      this.dispatched.push([name, payload]);
      this.emit(name, payload);
    }

    emit(name: string, payload: unknown) {
      (this.listeners.get(name) || []).forEach((listener) => listener(payload));
    }
  }

  class PDFLinkService {
    readonly options: unknown;
    readonly setViewer = vi.fn();
    readonly setDocument = vi.fn();
    readonly goToDestination = vi.fn().mockResolvedValue(undefined);

    constructor(options: unknown) {
      this.options = options;
      state.linkServices.push(this);
    }
  }

  class PDFFindController {
    constructor(readonly options: unknown) {}
  }

  class PDFViewer {
    readonly options: { eventBus: EventBus };
    currentPageNumber = 1;
    pdfDocument: unknown = null;
    /** 每次写入 currentScaleValue 的记录；PDF.js 每次写入都会按当前容器重算缩放。 */
    readonly scaleValueWrites: string[] = [];
    private scaleValue = 'auto';
    readonly increaseScale = vi.fn();
    readonly decreaseScale = vi.fn();
    readonly setDocument = vi.fn((nextDocument: unknown) => {
      this.pdfDocument = nextDocument;
      if (!nextDocument) return;
      this.currentPageNumber = 1;
      this.options.eventBus.emit('pagesinit', {});
    });

    constructor(options: { eventBus: EventBus }) {
      this.options = options;
      state.viewers.push(this);
    }

    get currentScaleValue() {
      return this.scaleValue;
    }

    set currentScaleValue(value: string) {
      this.scaleValue = value;
      this.scaleValueWrites.push(value);
    }
  }

  class ResizeObserverStub {
    readonly observe = vi.fn();
    readonly disconnect = vi.fn();

    constructor(callback: () => void) {
      state.resizeObservers.push({ trigger: callback, disconnect: this.disconnect });
    }
  }

  return {
    state,
    ResizeObserverStub,
    module: {
      EventBus,
      PDFLinkService,
      PDFFindController,
      PDFViewer,
      LinkTarget: { NONE: 0, SELF: 1, BLANK: 2 },
      FindState: { FOUND: 0, NOT_FOUND: 1, WRAPPED: 2, PENDING: 3 },
    },
  };
});

vi.mock('pdfjs-dist', () => ({
  getDocument: pdf.state.getDocument,
  GlobalWorkerOptions: { workerSrc: '' },
}));

vi.mock('pdfjs-dist/build/pdf.worker.min.mjs?url', () => ({
  default: '/assets/pdf.worker.test.mjs',
}));

vi.mock('pdfjs-dist/web/pdf_viewer.mjs', () => pdf.module);

const OUTLINE = [
  { title: '一、验收目的', dest: 'd1', items: [] },
  { title: '二、结构覆盖清单', dest: 'd2', items: [{ title: '2.1 列表样式检查', dest: 'd21', items: [] }] },
];

function pdfSource(): Blob {
  return { arrayBuffer: () => Promise.resolve(new Uint8Array([37, 80, 68, 70]).buffer) } as Blob;
}

function pdfDocument(numPages: number, outline: unknown) {
  return {
    numPages,
    getPage: vi.fn().mockResolvedValue({}),
    getOutline: vi.fn().mockResolvedValue(outline),
  };
}

function loadingTask(numPages = 3, outline: unknown = null) {
  const document = pdfDocument(numPages, outline);
  const task = {
    promise: Promise.resolve(document),
    destroy: vi.fn().mockResolvedValue(undefined),
  };
  return { document, task };
}

/** promise 由测试手动 resolve，用来观察“新文档解析中”的中间状态。 */
function deferredLoadingTask(numPages = 3) {
  const document = pdfDocument(numPages, null);
  let resolve!: () => void;
  const task = {
    promise: new Promise((settle) => { resolve = () => settle(document); }),
    destroy: vi.fn().mockResolvedValue(undefined),
  };
  return { document, task, resolve };
}

/** 必须在 getDocument 被调用时才创建 rejected promise，否则提前 reject 会成为未处理异常。 */
function failingTask() {
  return { promise: Promise.reject(new Error('Invalid PDF')), destroy: vi.fn().mockResolvedValue(undefined) };
}

type PreviewProps = Parameters<typeof DocumentPdfPreview>[0];

function renderPreview(overrides: Partial<PreviewProps> = {}) {
  const props: PreviewProps = {
    source: pdfSource(),
    sourceKey: 'report::render=pdf::1',
    requestId: 1,
    activeRequestId: 1,
    title: 'report.docx',
    onReady: vi.fn(),
    onError: vi.fn(),
    ...overrides,
  };
  return { props, ...render(<DocumentPdfPreview {...props} />) };
}

function refreshProps(props: PreviewProps): PreviewProps {
  return {
    ...props,
    source: pdfSource(),
    sourceKey: 'report::render=pdf::2',
    requestId: 2,
    activeRequestId: 2,
  };
}

function last<T>(items: T[]): T {
  return items[items.length - 1];
}

function eventBus() {
  return last(pdf.state.eventBuses);
}

function viewer() {
  return last(pdf.state.viewers);
}

describe('DocumentPdfPreview', () => {
  beforeEach(() => {
    pdf.state.getDocument.mockReset();
    pdf.state.eventBuses.length = 0;
    pdf.state.viewers.length = 0;
    pdf.state.linkServices.length = 0;
    pdf.state.resizeObservers.length = 0;
    vi.stubGlobal('ResizeObserver', pdf.ResizeObserverStub);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it('用 PDF.js 打开文档、默认适合宽度，且没有浏览器查看器或内部下载入口', async () => {
    const { document, task } = loadingTask();
    pdf.state.getDocument.mockReturnValue(task);

    const { props, container } = renderPreview();

    await waitFor(() => expect(props.onReady).toHaveBeenCalledWith('report::render=pdf::1'));
    expect(pdf.state.getDocument).toHaveBeenCalledWith({ data: expect.any(Uint8Array) });
    expect(viewer().setDocument).toHaveBeenCalledWith(document);
    expect(pdf.state.linkServices[0].setDocument).toHaveBeenCalledWith(document);
    expect(pdf.state.linkServices[0].options).toMatchObject({ externalLinkTarget: pdf.module.LinkTarget.BLANK });
    expect(viewer().currentScaleValue).toBe('page-width');
    expect(screen.getByText('/ 3')).toBeInTheDocument();
    expect(screen.queryByText('正在加载文档预览')).not.toBeInTheDocument();
    expect(container.querySelector('iframe')).toBeNull();
    expect(screen.queryByRole('button', { name: /下载/ })).toBeNull();
    expect(props.onError).not.toHaveBeenCalled();
  });

  it('工具栏缩放与页码跳转驱动 PDFViewer', async () => {
    pdf.state.getDocument.mockReturnValue(loadingTask().task);
    const { props } = renderPreview();
    await waitFor(() => expect(props.onReady).toHaveBeenCalled());

    fireEvent.click(screen.getByRole('button', { name: '放大' }));
    fireEvent.click(screen.getByRole('button', { name: '缩小' }));
    expect(viewer().increaseScale).toHaveBeenCalledTimes(1);
    expect(viewer().decreaseScale).toHaveBeenCalledTimes(1);

    act(() => eventBus().emit('scalechanging', { scale: 1.5 }));
    expect(screen.getByTestId('document-pdf-scale')).toHaveTextContent('150%');
    viewer().currentScaleValue = '1.5';
    fireEvent.click(screen.getByRole('button', { name: '适合宽度' }));
    expect(viewer().currentScaleValue).toBe('page-width');

    const pageInput = screen.getByRole('textbox', { name: '页码' });
    fireEvent.change(pageInput, { target: { value: '9' } });
    fireEvent.submit(pageInput.closest('form')!);
    expect(viewer().currentPageNumber).toBe(3);
    expect(pageInput).toHaveValue('3');

    act(() => eventBus().emit('pagechanging', { pageNumber: 2 }));
    expect(pageInput).toHaveValue('2');
  });

  it('搜索派发 find 事件并显示匹配计数，Esc 清除高亮', async () => {
    pdf.state.getDocument.mockReturnValue(loadingTask().task);
    const { props } = renderPreview();
    await waitFor(() => expect(props.onReady).toHaveBeenCalled());
    const search = screen.getByPlaceholderText('搜索文档');
    const lastFind = () => last(eventBus().dispatched.filter(([name]) => name === 'find'))[1];

    fireEvent.change(search, { target: { value: '合规' } });
    expect(lastFind()).toMatchObject({ type: '', query: '合规', highlightAll: true, findPrevious: false });

    act(() => eventBus().emit('updatefindcontrolstate', {
      state: pdf.module.FindState.FOUND,
      matchesCount: { current: 1, total: 4 },
    }));
    expect(screen.getByRole('status')).toHaveTextContent('1/4');

    fireEvent.keyDown(search, { key: 'Enter' });
    expect(lastFind()).toMatchObject({ type: 'again', query: '合规', findPrevious: false });
    fireEvent.keyDown(search, { key: 'Enter', shiftKey: true });
    expect(lastFind()).toMatchObject({ type: 'again', findPrevious: true });
    fireEvent.click(screen.getByRole('button', { name: '上一个匹配' }));
    expect(lastFind()).toMatchObject({ type: 'again', findPrevious: true });

    act(() => eventBus().emit('updatefindcontrolstate', {
      state: pdf.module.FindState.NOT_FOUND,
      matchesCount: { current: 0, total: 0 },
    }));
    expect(screen.getByRole('status')).toHaveTextContent('无结果');

    fireEvent.keyDown(search, { key: 'Escape' });
    expect(last(eventBus().dispatched)[0]).toBe('findbarclose');
    expect(search).toHaveValue('');
  });

  it('在文档区按 Ctrl+F 聚焦文档搜索框', async () => {
    pdf.state.getDocument.mockReturnValue(loadingTask().task);
    const { props } = renderPreview();
    await waitFor(() => expect(props.onReady).toHaveBeenCalled());

    fireEvent.keyDown(screen.getByTestId('document-pdf-scroller'), { key: 'f', ctrlKey: true });

    expect(screen.getByPlaceholderText('搜索文档')).toHaveFocus();
  });

  it('搜索组以真实最小宽度参与工具栏换行，窄栏时不会被压成零宽', async () => {
    // jsdom 不计算布局；真实浏览器中 min-w-0 会让整组永不换行、搜索框宽度归零（已在浏览器复现）。
    pdf.state.getDocument.mockReturnValue(loadingTask().task);
    const { props } = renderPreview();
    await waitFor(() => expect(props.onReady).toHaveBeenCalled());

    const group = screen.getByTestId('document-pdf-search-group');
    expect(group).toContainElement(screen.getByPlaceholderText('搜索文档'));
    expect(group).toHaveClass('min-w-[180px]');
    expect(group).not.toHaveClass('min-w-0');
  });

  it('标题索引：有书签时显示“目录”，展开后点击标题跳转到对应位置', async () => {
    pdf.state.getDocument.mockReturnValue(loadingTask(3, OUTLINE).task);
    const { props } = renderPreview();
    await waitFor(() => expect(props.onReady).toHaveBeenCalled());

    const toggle = screen.getByRole('button', { name: '目录' });
    expect(toggle).toHaveAttribute('aria-pressed', 'false');
    expect(screen.queryByRole('navigation', { name: '文档目录' })).toBeNull();

    fireEvent.click(toggle);
    expect(toggle).toHaveAttribute('aria-pressed', 'true');
    expect(screen.getByRole('navigation', { name: '文档目录' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '一、验收目的' })).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: '2.1 列表样式检查' }));
    expect(pdf.state.linkServices[0].goToDestination).toHaveBeenCalledWith('d21');

    fireEvent.click(toggle);
    expect(screen.queryByRole('navigation', { name: '文档目录' })).toBeNull();
  });

  it('文档没有标题书签时不显示“目录”按钮', async () => {
    pdf.state.getDocument.mockReturnValue(loadingTask(3, null).task);
    const { props } = renderPreview();
    await waitFor(() => expect(props.onReady).toHaveBeenCalled());

    expect(screen.queryByRole('button', { name: '目录' })).toBeNull();
  });

  it('后台刷新期间旧文档持续显示，新文档读到第一页后才切换', async () => {
    const first = loadingTask(3);
    const second = deferredLoadingTask(5);
    pdf.state.getDocument.mockReturnValueOnce(first.task).mockReturnValueOnce(second.task);
    const { props, rerender } = renderPreview();
    await waitFor(() => expect(props.onReady).toHaveBeenCalledWith('report::render=pdf::1'));

    rerender(<DocumentPdfPreview {...refreshProps(props)} />);
    await waitFor(() => expect(pdf.state.getDocument).toHaveBeenCalledTimes(2));

    expect(viewer().pdfDocument).toBe(first.document);
    expect(viewer().setDocument).not.toHaveBeenCalledWith(null);
    expect(screen.queryByText('正在加载文档预览')).toBeNull();
    expect(first.task.destroy).not.toHaveBeenCalled();

    await act(async () => {
      second.resolve();
    });

    await waitFor(() => expect(props.onReady).toHaveBeenCalledWith('report::render=pdf::2'));
    expect(pdf.state.viewers).toHaveLength(1);
    expect(viewer().setDocument.mock.calls.map(([document]: [unknown]) => document)).toEqual([
      first.document,
      second.document,
    ]);
    expect(first.task.destroy).toHaveBeenCalled();
    expect(second.task.destroy).not.toHaveBeenCalled();
    expect(screen.getByText('/ 5')).toBeInTheDocument();
  });

  it('解析中的刷新请求被更新请求取代后，迟到的解析结果不替换正文', async () => {
    const first = loadingTask(3);
    const stale = deferredLoadingTask(5);
    const latest = loadingTask(4);
    pdf.state.getDocument
      .mockReturnValueOnce(first.task)
      .mockReturnValueOnce(stale.task)
      .mockReturnValueOnce(latest.task);
    const { props, rerender } = renderPreview();
    await waitFor(() => expect(props.onReady).toHaveBeenCalledWith('report::render=pdf::1'));
    const staleProps = refreshProps(props);
    rerender(<DocumentPdfPreview {...staleProps} />);
    await waitFor(() => expect(pdf.state.getDocument).toHaveBeenCalledTimes(2));

    // FilePreview 已发起第 3 次请求，新 PDF 尚未下载完成，仍把第 2 次的来源传下来。
    rerender(<DocumentPdfPreview {...staleProps} activeRequestId={3} />);
    await act(async () => {
      stale.resolve();
    });

    await waitFor(() => expect(stale.task.destroy).toHaveBeenCalled());
    expect(viewer().pdfDocument).toBe(first.document);
    expect(viewer().setDocument).toHaveBeenCalledTimes(1);
    expect(first.task.destroy).not.toHaveBeenCalled();
    expect(screen.getByText('/ 3')).toBeInTheDocument();
    expect(props.onReady).not.toHaveBeenCalledWith('report::render=pdf::2');
    expect(props.onError).not.toHaveBeenCalled();

    rerender(
      <DocumentPdfPreview
        {...staleProps}
        source={pdfSource()}
        sourceKey="report::render=pdf::3"
        requestId={3}
        activeRequestId={3}
      />,
    );
    await waitFor(() => expect(props.onReady).toHaveBeenCalledWith('report::render=pdf::3'));
    expect(viewer().setDocument.mock.calls.map(([document]: [unknown]) => document)).toEqual([
      first.document,
      latest.document,
    ]);
    expect(screen.getByText('/ 4')).toBeInTheDocument();
  });

  it('后台刷新的新版本无法解析时保留旧文档并回报错误，不重新解析旧文档', async () => {
    const first = loadingTask(3);
    let failing!: ReturnType<typeof failingTask>;
    pdf.state.getDocument
      .mockReturnValueOnce(first.task)
      .mockImplementationOnce(() => (failing = failingTask()));
    vi.spyOn(console, 'error').mockImplementation(() => undefined);
    const { props, rerender } = renderPreview();
    await waitFor(() => expect(props.onReady).toHaveBeenCalledWith('report::render=pdf::1'));

    rerender(<DocumentPdfPreview {...refreshProps(props)} />);

    await waitFor(() => expect(props.onError).toHaveBeenCalledWith(expect.any(Error), 'report::render=pdf::2'));
    expect(pdf.state.getDocument).toHaveBeenCalledTimes(2);
    expect(viewer().pdfDocument).toBe(first.document);
    expect(viewer().setDocument).toHaveBeenCalledTimes(1);
    expect(failing.destroy).toHaveBeenCalled();
    expect(first.task.destroy).not.toHaveBeenCalled();
    expect(screen.getByText('/ 3')).toBeInTheDocument();
    expect(screen.queryByText('正在加载文档预览')).toBeNull();
  });

  it('PDF.js 打开失败时回报错误且不提交 ready', async () => {
    let failing!: ReturnType<typeof failingTask>;
    pdf.state.getDocument.mockImplementation(() => (failing = failingTask()));
    vi.spyOn(console, 'error').mockImplementation(() => undefined);

    const { props } = renderPreview();

    await waitFor(() => expect(props.onError).toHaveBeenCalledWith(expect.any(Error), 'report::render=pdf::1'));
    expect(props.onReady).not.toHaveBeenCalled();
    expect(failing.destroy).toHaveBeenCalled();
    expect(screen.getByText('正在加载文档预览')).toBeInTheDocument();
  });

  it('来源不属于当前请求时不加载', async () => {
    renderPreview({ requestId: 1, activeRequestId: 2 });
    await act(async () => {
      await Promise.resolve();
    });
    expect(pdf.state.getDocument).not.toHaveBeenCalled();
  });

  it('在隐藏分页中加载后，恢复显示时重算适合宽度；滚动条等宽度变化与手动缩放不触发重算', async () => {
    // jsdom 中 clientWidth 恒为 0，对应分页以 display:none 隐藏时挂载并完成加载。
    pdf.state.getDocument.mockReturnValue(loadingTask().task);
    const { props } = renderPreview();
    await waitFor(() => expect(props.onReady).toHaveBeenCalled());
    const scroller = screen.getByTestId('document-pdf-scroller');
    const observer = last(pdf.state.resizeObservers);
    const setWidth = (value: number) => Object.defineProperty(scroller, 'clientWidth', { configurable: true, value });
    expect(viewer().scaleValueWrites).toEqual(['page-width']);

    observer.trigger();
    expect(viewer().scaleValueWrites).toEqual(['page-width']);

    setWidth(800);
    observer.trigger();
    expect(viewer().scaleValueWrites).toEqual(['page-width', 'page-width']);

    // 显示中的宽度变化（竖向滚动条出现/消失、拖动分栏）不重算。
    setWidth(785);
    observer.trigger();
    expect(viewer().scaleValueWrites).toEqual(['page-width', 'page-width']);

    setWidth(0);
    observer.trigger();
    viewer().currentScaleValue = '1.5';
    setWidth(800);
    observer.trigger();
    expect(viewer().scaleValueWrites).toEqual(['page-width', 'page-width', '1.5']);
  });

  it('卸载时清空查看器并销毁文档，释放 PDF.js 持有的页面', async () => {
    const { task } = loadingTask();
    pdf.state.getDocument.mockReturnValue(task);
    const { props, unmount } = renderPreview();
    await waitFor(() => expect(props.onReady).toHaveBeenCalled());

    unmount();

    expect(viewer().setDocument).toHaveBeenLastCalledWith(null);
    expect(pdf.state.linkServices[0].setDocument).toHaveBeenLastCalledWith(null);
    expect(task.destroy).toHaveBeenCalled();
    expect(last(pdf.state.resizeObservers).disconnect).toHaveBeenCalled();
  });

  it('解析中卸载时作废结果并销毁未完成的文档', async () => {
    const pending = deferredLoadingTask();
    pdf.state.getDocument.mockReturnValue(pending.task);
    const { props, unmount } = renderPreview();
    await waitFor(() => expect(pdf.state.getDocument).toHaveBeenCalled());

    unmount();
    await act(async () => {
      pending.resolve();
    });

    await waitFor(() => expect(pending.task.destroy).toHaveBeenCalled());
    expect(pdf.state.viewers).toHaveLength(0);
    expect(props.onReady).not.toHaveBeenCalled();
  });
});
