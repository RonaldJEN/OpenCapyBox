import {
  useEffect,
  useRef,
  useState,
  type FormEvent,
  type KeyboardEvent,
} from 'react';
import {
  ChevronDown,
  ChevronUp,
  List,
  Loader2,
  Minus,
  MoveHorizontal,
  Plus,
  Search,
} from 'lucide-react';
import type { PDFDocumentLoadingTask, PDFDocumentProxy } from 'pdfjs-dist';
import type { EventBus, PDFLinkService, PDFViewer } from 'pdfjs-dist/web/pdf_viewer.mjs';
import { loadPdfJs, loadPdfViewer } from './pdfJsLoader';
import './DocumentPdfPreview.css';

interface DocumentPdfPreviewProps {
  source: Blob;
  sourceKey: string;
  requestId: number;
  activeRequestId: number;
  title: string;
  onReady: (sourceKey: string) => void;
  onError: (error: Error, sourceKey: string) => void;
}

interface ViewerRuntime {
  eventBus: EventBus;
  linkService: PDFLinkService;
  viewer: PDFViewer;
}

interface FindMatches {
  current: number;
  total: number;
}

interface OutlineNode {
  title: string;
  dest: string | unknown[] | null;
  items: OutlineNode[];
}

type PdfViewerModule = Awaited<ReturnType<typeof loadPdfViewer>>;

const NO_MATCHES: FindMatches = { current: 0, total: 0 };
const toolbarButtonClassName = 'inline-flex h-7 w-7 shrink-0 items-center justify-center rounded-md text-claude-muted transition-colors hover:bg-claude-hover hover:text-claude-text disabled:pointer-events-none disabled:opacity-40';
const toggleButtonClassName = (pressed: boolean) => `inline-flex h-7 shrink-0 items-center gap-1 rounded-md px-2 text-xs transition-colors disabled:pointer-events-none disabled:opacity-40 ${pressed ? 'bg-claude-accent/10 text-claude-accent' : 'text-claude-muted hover:bg-claude-hover hover:text-claude-text'}`;

function OutlineList({
  items,
  depth,
  onSelect,
}: {
  items: OutlineNode[];
  depth: number;
  onSelect: (dest: string | unknown[]) => void;
}) {
  return (
    <ul>
      {items.map((item, index) => (
        <li key={`${depth}:${index}:${item.title}`}>
          <button
            type="button"
            disabled={item.dest === null}
            onClick={() => onSelect(item.dest!)}
            className="block w-full truncate rounded-md py-1.5 pr-2 text-left text-xs text-claude-secondary hover:bg-claude-hover hover:text-claude-text disabled:pointer-events-none disabled:opacity-50"
            style={{ paddingLeft: `${8 + depth * 12}px` }}
            title={item.title}
          >
            {item.title}
          </button>
          {item.items.length > 0 && <OutlineList items={item.items} depth={depth + 1} onSelect={onSelect} />}
        </li>
      ))}
    </ul>
  );
}

/**
 * Office 派生 PDF 的只读查看器。用 PDF.js 自绘页面与文字层，不使用浏览器内置 PDF
 * 查看器，因此没有第二个“下载”入口，也不会显示 PDF 元数据里的旧标题。
 */
export function DocumentPdfPreview({
  source,
  sourceKey,
  requestId,
  activeRequestId,
  title,
  onReady,
  onError,
}: DocumentPdfPreviewProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const viewerRef = useRef<HTMLDivElement>(null);
  const searchInputRef = useRef<HTMLInputElement>(null);
  const runtimeRef = useRef<ViewerRuntime | null>(null);
  const activeTaskRef = useRef<PDFDocumentLoadingTask | null>(null);
  // 每次打开文档或请求失效时递增；递增后进行中的解析结果作废。
  const loadTokenRef = useRef(0);
  const onReadyRef = useRef(onReady);
  const onErrorRef = useRef(onError);
  onReadyRef.current = onReady;
  onErrorRef.current = onError;

  const [hasDocument, setHasDocument] = useState(false);
  const [pagesCount, setPagesCount] = useState(0);
  const [pageNumber, setPageNumber] = useState(1);
  const [pageDraft, setPageDraft] = useState('1');
  const [scalePercent, setScalePercent] = useState(100);
  const [query, setQuery] = useState('');
  const [matches, setMatches] = useState<FindMatches>(NO_MATCHES);
  const [notFound, setNotFound] = useState(false);
  const [outline, setOutline] = useState<OutlineNode[]>([]);
  const [outlineOpen, setOutlineOpen] = useState(false);

  const createRuntime = (viewerModule: PdfViewerModule): ViewerRuntime => {
    const eventBus = new viewerModule.EventBus();
    const linkService = new viewerModule.PDFLinkService({
      eventBus,
      externalLinkTarget: viewerModule.LinkTarget.BLANK,
    });
    const findController = new viewerModule.PDFFindController({ eventBus, linkService });
    const viewer = new viewerModule.PDFViewer({
      container: containerRef.current!,
      viewer: viewerRef.current!,
      eventBus,
      linkService,
      findController,
    });
    linkService.setViewer(viewer);
    eventBus.on('pagesinit', () => {
      viewer.currentScaleValue = 'page-width';
    });
    eventBus.on('pagechanging', ({ pageNumber: nextPage }: { pageNumber: number }) => {
      setPageNumber(nextPage);
      setPageDraft(String(nextPage));
    });
    eventBus.on('scalechanging', ({ scale }: { scale: number }) => {
      setScalePercent(Math.round(scale * 100));
    });
    eventBus.on('updatefindmatchescount', ({ matchesCount }: { matchesCount: FindMatches }) => {
      setMatches(matchesCount);
    });
    eventBus.on('updatefindcontrolstate', ({ state, matchesCount }: { state: number; matchesCount: FindMatches }) => {
      setMatches(matchesCount);
      setNotFound(state === viewerModule.FindState.NOT_FOUND);
    });
    return { eventBus, linkService, viewer };
  };

  /**
   * 解析一份 PDF 并替换当前文档；新文档读到第一页前旧文档保持显示，失败时旧文档原样保留。
   * 被更晚的打开取代时返回 false。
   */
  const openDocument = async (blob: Blob): Promise<boolean> => {
    loadTokenRef.current += 1;
    const token = loadTokenRef.current;
    const [pdfJs, viewerModule, payload] = await Promise.all([
      loadPdfJs(),
      loadPdfViewer(),
      blob.arrayBuffer(),
    ]);
    if (token !== loadTokenRef.current) return false;

    const loadingTask = pdfJs.getDocument({ data: new Uint8Array(payload) });
    let promoted = false;
    try {
      const pdfDocument = await loadingTask.promise;
      const [, documentOutline] = await Promise.all([
        pdfDocument.getPage(1),
        pdfDocument.getOutline(),
      ]);
      if (token !== loadTokenRef.current) return false;

      runtimeRef.current ??= createRuntime(viewerModule);
      runtimeRef.current.viewer.setDocument(pdfDocument);
      runtimeRef.current.linkService.setDocument(pdfDocument);
      void activeTaskRef.current?.destroy();
      activeTaskRef.current = loadingTask;
      promoted = true;
      setPagesCount(pdfDocument.numPages);
      setPageNumber(1);
      setPageDraft('1');
      // PDF 没有书签时 getOutline() 返回 null。
      setOutline(documentOutline ?? []);
      setQuery('');
      setMatches(NO_MATCHES);
      setNotFound(false);
      setHasDocument(true);
      return true;
    } finally {
      if (!promoted) void loadingTask.destroy();
    }
  };

  useEffect(() => {
    if (requestId !== activeRequestId) return undefined;

    let disposed = false;
    openDocument(source)
      .then((opened) => {
        if (!opened || disposed) return;
        onReadyRef.current(sourceKey);
      })
      .catch((error: unknown) => {
        if (disposed) return;
        const nextError = error instanceof Error ? error : new Error(String(error));
        console.error('Failed to load document PDF preview:', nextError);
        onErrorRef.current(nextError, sourceKey);
      });

    return () => {
      disposed = true;
      // 来源换新或被更新的请求取代（activeRequestId 变化、新 PDF 尚未到达）时，进行中的解析
      // 不得再替换正文：作废 token，迟到结果在检查点被丢弃并销毁其 loading task。
      loadTokenRef.current += 1;
    };
  // openDocument 只读取 ref 与稳定的 state setter。
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeRequestId, requestId, source, sourceKey]);

  useEffect(() => {
    const container = containerRef.current!;
    let visibleWidth = container.clientWidth;
    // 分页以 display:none 保活时容器宽度为 0，此时套用的 page-width 是负缩放；只在从隐藏
    // 恢复显示时按预设模式重算，用户手动缩放保持不变。其他宽度变化不重算：页面缩放会让
    // 竖向滚动条出现/消失，跟着重算会打断首次渲染并来回调整缩放。
    const observer = new ResizeObserver(() => {
      const wasHidden = visibleWidth === 0;
      visibleWidth = container.clientWidth;
      const viewer = runtimeRef.current?.viewer;
      if (!wasHidden || visibleWidth === 0 || !viewer?.pdfDocument) return;
      if (viewer.currentScaleValue === 'page-width') viewer.currentScaleValue = 'page-width';
    });
    observer.observe(container);
    return () => {
      observer.disconnect();
      loadTokenRef.current += 1;
      // pdf.js 的 TextLayerBuilder 用静态 Map 登记文字层，只有页面重置时才移除；
      // 卸载前必须清空查看器，否则已渲染页面会一直被持有。
      runtimeRef.current?.viewer.setDocument(null as unknown as PDFDocumentProxy);
      runtimeRef.current?.linkService.setDocument(null);
      void activeTaskRef.current?.destroy();
    };
  }, []);

  const runtime = () => runtimeRef.current!;

  const goToPage = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const requested = Number.parseInt(pageDraft, 10);
    const target = Number.isNaN(requested)
      ? pageNumber
      : Math.min(Math.max(requested, 1), pagesCount);
    runtime().viewer.currentPageNumber = target;
    setPageDraft(String(target));
  };

  const find = (type: '' | 'again', nextQuery: string, findPrevious = false) => {
    runtime().eventBus.dispatch('find', {
      source: null,
      type,
      query: nextQuery,
      caseSensitive: false,
      entireWord: false,
      highlightAll: true,
      findPrevious,
      matchDiacritics: false,
    });
  };

  const updateQuery = (nextQuery: string) => {
    setQuery(nextQuery);
    if (nextQuery) {
      find('', nextQuery);
      return;
    }
    runtime().eventBus.dispatch('findbarclose', { source: null });
    setMatches(NO_MATCHES);
    setNotFound(false);
  };

  const handleSearchKeyDown = (event: KeyboardEvent<HTMLInputElement>) => {
    if (event.key === 'Enter' && query) {
      event.preventDefault();
      find('again', query, event.shiftKey);
    } else if (event.key === 'Escape' && query) {
      event.preventDefault();
      updateQuery('');
    }
  };

  const handleShortcut = (event: KeyboardEvent<HTMLDivElement>) => {
    if (hasDocument && (event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'f') {
      event.preventDefault();
      searchInputRef.current!.focus();
      searchInputRef.current!.select();
    }
  };

  return (
    <div className="document-pdf-preview" aria-label={`${title} 文档预览`} onKeyDown={handleShortcut}>
      <div className="document-pdf-preview__toolbar" role="toolbar" aria-label="文档预览工具栏">
        {outline.length > 0 && (
          <button
            type="button"
            className={toggleButtonClassName(outlineOpen)}
            aria-label="目录"
            aria-pressed={outlineOpen}
            title={outlineOpen ? '收起目录' : '展开目录'}
            onClick={() => setOutlineOpen((open) => !open)}
          >
            <List size={15} aria-hidden="true" />
            目录
          </button>
        )}
        <div className="flex items-center gap-0.5">
          <button type="button" className={toolbarButtonClassName} aria-label="缩小" title="缩小" disabled={!hasDocument} onClick={() => runtime().viewer.decreaseScale()}>
            <Minus size={15} aria-hidden="true" />
          </button>
          <span className="w-11 text-center text-xs tabular-nums text-claude-secondary" data-testid="document-pdf-scale">{scalePercent}%</span>
          <button type="button" className={toolbarButtonClassName} aria-label="放大" title="放大" disabled={!hasDocument} onClick={() => runtime().viewer.increaseScale()}>
            <Plus size={15} aria-hidden="true" />
          </button>
          <button type="button" className={toolbarButtonClassName} aria-label="适合宽度" title="适合宽度" disabled={!hasDocument} onClick={() => { runtime().viewer.currentScaleValue = 'page-width'; }}>
            <MoveHorizontal size={15} aria-hidden="true" />
          </button>
        </div>
        <form className="flex items-center gap-1 text-xs text-claude-secondary" onSubmit={goToPage}>
          <input
            aria-label="页码"
            inputMode="numeric"
            value={pageDraft}
            disabled={!hasDocument}
            onChange={(event) => setPageDraft(event.target.value)}
            onBlur={() => setPageDraft(String(pageNumber))}
            className="h-7 w-10 rounded-md border border-claude-border bg-white text-center tabular-nums text-claude-text outline-none focus:border-claude-accent"
          />
          <span className="tabular-nums">/ {pagesCount}</span>
        </form>
        {/* 工具栏 flex-wrap 按最小宽度判断换行：min-w-0 会让这组永不换行而把搜索框压成零宽，窄栏时应整组换到下一行。 */}
        <div className="flex min-w-[180px] flex-1 items-center justify-end gap-0.5" data-testid="document-pdf-search-group">
          <label className="flex h-7 min-w-0 max-w-[220px] flex-1 items-center gap-1.5 rounded-md bg-claude-surface px-2 text-claude-muted">
            <Search size={13} aria-hidden="true" />
            <span className="sr-only">搜索文档</span>
            <input
              ref={searchInputRef}
              type="search"
              value={query}
              disabled={!hasDocument}
              placeholder="搜索文档"
              onChange={(event) => updateQuery(event.target.value)}
              onKeyDown={handleSearchKeyDown}
              className="min-w-0 flex-1 bg-transparent text-xs text-claude-text outline-none"
            />
          </label>
          {query && (
            <span className="shrink-0 px-1 text-xs tabular-nums text-claude-muted" role="status" aria-live="polite">
              {notFound ? '无结果' : `${matches.current}/${matches.total}`}
            </span>
          )}
          <button type="button" className={toolbarButtonClassName} aria-label="上一个匹配" title="上一个匹配（Shift+Enter）" disabled={!query} onClick={() => find('again', query, true)}>
            <ChevronUp size={15} aria-hidden="true" />
          </button>
          <button type="button" className={toolbarButtonClassName} aria-label="下一个匹配" title="下一个匹配（Enter）" disabled={!query} onClick={() => find('again', query)}>
            <ChevronDown size={15} aria-hidden="true" />
          </button>
        </div>
      </div>
      <div className="document-pdf-preview__stage">
        {outlineOpen && outline.length > 0 && (
          <nav className="document-pdf-preview__outline" aria-label="文档目录">
            <OutlineList
              items={outline}
              depth={0}
              onSelect={(dest) => void runtime().linkService.goToDestination(dest)}
            />
          </nav>
        )}
        <div className="document-pdf-preview__canvas">
          {/* PDF.js 要求滚动容器绝对定位；页面节点由 PDFViewer 写入 viewer div，不受 React 管理。 */}
          <div
            ref={containerRef}
            className="document-pdf-preview__scroller"
            style={{ position: 'absolute', inset: 0, overflow: 'auto' }}
            tabIndex={0}
            data-testid="document-pdf-scroller"
          >
            <div ref={viewerRef} className="pdfViewer" />
          </div>
        </div>
        {!hasDocument && (
          <div className="document-pdf-preview__loading" role="status" aria-live="polite">
            <Loader2 className="animate-spin" size={22} aria-hidden="true" />
            <span>正在加载文档预览</span>
          </div>
        )}
      </div>
    </div>
  );
}
