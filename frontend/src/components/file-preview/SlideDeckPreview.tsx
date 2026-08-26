import {
  useEffect,
  useMemo,
  useRef,
  useState,
  type RefObject,
} from 'react';
import { AlertTriangle, Loader2, Presentation } from 'lucide-react';
import type {
  PDFDocumentLoadingTask,
  PDFDocumentProxy,
  RenderTask,
} from 'pdfjs-dist';
import './SlideDeckPreview.css';

interface SlideDeckPreviewProps {
  src: string;
  title: string;
  onError?: (error: Error) => void;
}

interface SlideCanvasProps {
  document: PDFDocumentProxy;
  pageNumber: number;
  variant: 'page' | 'thumbnail';
}

interface LazySlideThumbnailProps {
  active: boolean;
  aspectRatio: number;
  document: PDFDocumentProxy;
  pageNumber: number;
  scrollRootRef: RefObject<HTMLDivElement>;
  onSelect: (pageNumber: number) => void;
}

type PdfJs = typeof import('pdfjs-dist');

let pdfJsPromise: Promise<PdfJs> | null = null;

function loadPdfJs(): Promise<PdfJs> {
  if (!pdfJsPromise) {
    pdfJsPromise = Promise.all([
      import('pdfjs-dist'),
      import('pdfjs-dist/build/pdf.worker.min.mjs?url'),
    ])
      .then(([pdfJs, workerModule]) => {
        if (!pdfJs.GlobalWorkerOptions.workerSrc) {
          pdfJs.GlobalWorkerOptions.workerSrc = workerModule.default;
        }
        return pdfJs;
      })
      .catch((error: unknown) => {
        pdfJsPromise = null;
        throw error;
      });
  }
  return pdfJsPromise;
}

function getError(error: unknown): Error {
  return error instanceof Error ? error : new Error(String(error));
}

function isCancelledRender(error: unknown): boolean {
  return error instanceof Error && error.name === 'RenderingCancelledException';
}

function prefersReducedMotion(): boolean {
  return typeof window.matchMedia === 'function'
    && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
}

function useElementWidth(elementRef: RefObject<HTMLElement>): number {
  const [width, setWidth] = useState(0);

  useEffect(() => {
    const element = elementRef.current;
    if (!element) return undefined;

    const measure = () => {
      const nextWidth = element.getBoundingClientRect().width || element.clientWidth;
      if (nextWidth > 0) setWidth(nextWidth);
    };

    measure();
    if (typeof ResizeObserver === 'undefined') {
      window.addEventListener('resize', measure);
      return () => window.removeEventListener('resize', measure);
    }

    const observer = new ResizeObserver((entries) => {
      const observedWidth = entries[0]?.contentRect.width || element.clientWidth;
      if (observedWidth > 0) setWidth(observedWidth);
    });
    observer.observe(element);
    return () => observer.disconnect();
  }, [elementRef]);

  return width;
}

function SlideCanvas({ document, pageNumber, variant }: SlideCanvasProps) {
  const frameRef = useRef<HTMLDivElement>(null);
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const width = useElementWidth(frameRef);
  const [status, setStatus] = useState<'loading' | 'ready' | 'error'>('loading');

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas || width <= 0) return undefined;

    let cancelled = false;
    let renderTask: RenderTask | null = null;
    setStatus('loading');

    void document.getPage(pageNumber)
      .then((page) => {
        if (cancelled) return null;
        const baseViewport = page.getViewport({ scale: 1 });
        const cssScale = width / baseViewport.width;
        const dpr = variant === 'page'
          ? Math.min(Math.max(window.devicePixelRatio || 1, 1), 2)
          : 1;
        const renderViewport = page.getViewport({ scale: cssScale * dpr });
        const cssHeight = baseViewport.height * cssScale;

        canvas.width = Math.max(1, Math.floor(renderViewport.width));
        canvas.height = Math.max(1, Math.floor(renderViewport.height));
        canvas.style.width = `${width}px`;
        canvas.style.height = `${cssHeight}px`;

        renderTask = page.render({
          canvas,
          viewport: renderViewport,
          background: '#ffffff',
        });
        return renderTask.promise;
      })
      .then(() => {
        if (!cancelled) setStatus('ready');
      })
      .catch((error: unknown) => {
        if (!cancelled && !isCancelledRender(error)) {
          console.error(`Failed to render slide ${pageNumber}:`, error);
          setStatus('error');
        }
      });

    return () => {
      cancelled = true;
      renderTask?.cancel();
    };
  }, [document, pageNumber, variant, width]);

  return (
    <div
      ref={frameRef}
      className={`slide-deck-preview__canvas-frame slide-deck-preview__canvas-frame--${variant}`}
      role="img"
      aria-label={`第 ${pageNumber} 页`}
      aria-busy={status === 'loading'}
    >
      {status === 'loading' && <div className="slide-deck-preview__page-skeleton" aria-hidden="true" />}
      {status === 'error' && (
        <div className="slide-deck-preview__page-error" role="alert">
          第 {pageNumber} 页渲染失败
        </div>
      )}
      <canvas
        ref={canvasRef}
        className="slide-deck-preview__canvas"
        data-slide-canvas={variant}
        data-page-number={pageNumber}
        aria-hidden="true"
        style={{ visibility: status === 'ready' ? 'visible' : 'hidden' }}
      />
    </div>
  );
}

function LazySlideThumbnail({
  active,
  aspectRatio,
  document,
  pageNumber,
  scrollRootRef,
  onSelect,
}: LazySlideThumbnailProps) {
  const buttonRef = useRef<HTMLButtonElement>(null);
  const [visible, setVisible] = useState(false);

  useEffect(() => {
    const button = buttonRef.current;
    if (!button) return undefined;

    if (typeof IntersectionObserver === 'undefined') {
      setVisible(true);
      return undefined;
    }

    const observer = new IntersectionObserver((entries) => {
      if (entries.some((entry) => entry.isIntersecting)) {
        setVisible(true);
        observer.disconnect();
      }
    }, {
      root: scrollRootRef.current,
      rootMargin: '180px',
      threshold: 0.01,
    });
    observer.observe(button);
    return () => observer.disconnect();
  }, [scrollRootRef]);

  return (
    <button
      ref={buttonRef}
      type="button"
      className="slide-deck-preview__thumbnail-button"
      data-page-number={pageNumber}
      aria-label={`转到第 ${pageNumber} 页`}
      aria-current={active ? 'page' : undefined}
      onClick={() => onSelect(pageNumber)}
    >
      <span className="slide-deck-preview__thumbnail-number" aria-hidden="true">{pageNumber}</span>
      <span className="slide-deck-preview__thumbnail-frame" style={{ aspectRatio: String(aspectRatio) }}>
        {visible ? (
          <SlideCanvas document={document} pageNumber={pageNumber} variant="thumbnail" />
        ) : (
          <span className="slide-deck-preview__thumbnail-placeholder" aria-hidden="true" />
        )}
      </span>
    </button>
  );
}

function SlideDeckLoading() {
  return (
    <div className="slide-deck-preview slide-deck-preview--message" role="status" aria-live="polite">
      <Loader2 className="slide-deck-preview__loader" size={24} aria-hidden="true" />
      <strong>正在准备幻灯片</strong>
      <span>首次打开时需要加载 PDF 渲染器</span>
    </div>
  );
}

function SlideDeckFallback({ src, title }: Pick<SlideDeckPreviewProps, 'src' | 'title'>) {
  return (
    <div className="slide-deck-preview slide-deck-preview--fallback">
      <div className="slide-deck-preview__fallback-notice" role="alert">
        <AlertTriangle size={17} aria-hidden="true" />
        <span>高级幻灯片浏览器加载失败，已切换为浏览器 PDF 预览。</span>
      </div>
      <iframe
        src={src}
        className="slide-deck-preview__fallback-frame"
        title={`${title} PDF 降级预览`}
      />
    </div>
  );
}

export function SlideDeckPreview({ src, title, onError }: SlideDeckPreviewProps) {
  const [document, setDocument] = useState<PDFDocumentProxy | null>(null);
  const [aspectRatio, setAspectRatio] = useState(16 / 9);
  const [currentPage, setCurrentPage] = useState(1);
  const [failed, setFailed] = useState(false);
  const thumbnailScrollRef = useRef<HTMLDivElement>(null);
  const pageScrollRef = useRef<HTMLDivElement>(null);
  const onErrorRef = useRef(onError);

  useEffect(() => {
    onErrorRef.current = onError;
  }, [onError]);

  useEffect(() => {
    let disposed = false;
    let loadingTask: PDFDocumentLoadingTask | null = null;

    setDocument(null);
    setAspectRatio(16 / 9);
    setCurrentPage(1);
    setFailed(false);

    void loadPdfJs()
      .then((pdfJs) => {
        if (disposed) return null;
        loadingTask = pdfJs.getDocument({ url: src });
        return loadingTask.promise;
      })
      .then(async (nextDocument) => {
        if (!nextDocument) return;
        if (disposed) return;

        const firstPage = await nextDocument.getPage(1);
        const viewport = firstPage.getViewport({ scale: 1 });
        if (disposed) return;
        setAspectRatio(viewport.width / viewport.height);
        setDocument(nextDocument);
      })
      .catch((error: unknown) => {
        if (disposed) return;
        const nextError = getError(error);
        console.error('Failed to load slide deck preview:', nextError);
        setFailed(true);
        onErrorRef.current?.(nextError);
      });

    return () => {
      disposed = true;
      if (loadingTask) void loadingTask.destroy();
    };
  }, [src]);

  const pageNumbers = useMemo(
    () => Array.from({ length: document?.numPages || 0 }, (_, index) => index + 1),
    [document],
  );

  useEffect(() => {
    const root = pageScrollRef.current;
    if (!document || !root) return undefined;
    const pages = Array.from(root.querySelectorAll<HTMLElement>('[data-slide-page]'));
    if (pages.length === 0) return undefined;

    if (typeof IntersectionObserver === 'undefined') {
      let frame = 0;
      const updateCurrentPage = () => {
        frame = 0;
        const rootRect = root.getBoundingClientRect();
        const focusY = rootRect.top + rootRect.height * 0.38;
        let bestPage = 1;
        let bestDistance = Number.POSITIVE_INFINITY;
        pages.forEach((page) => {
          const rect = page.getBoundingClientRect();
          const distance = Math.abs(rect.top + rect.height / 2 - focusY);
          if (distance < bestDistance) {
            bestDistance = distance;
            bestPage = Number(page.dataset.pageNumber) || 1;
          }
        });
        setCurrentPage(bestPage);
      };
      const onScroll = () => {
        if (!frame) frame = window.requestAnimationFrame(updateCurrentPage);
      };
      root.addEventListener('scroll', onScroll, { passive: true });
      updateCurrentPage();
      return () => {
        root.removeEventListener('scroll', onScroll);
        if (frame) window.cancelAnimationFrame(frame);
      };
    }

    const visibility = new Map<number, number>();
    const observer = new IntersectionObserver((entries) => {
      entries.forEach((entry) => {
        const pageNumber = Number((entry.target as HTMLElement).dataset.pageNumber);
        visibility.set(pageNumber, entry.isIntersecting ? entry.intersectionRatio : 0);
      });

      let bestPage = 0;
      let bestRatio = 0;
      visibility.forEach((ratio, pageNumber) => {
        if (ratio > bestRatio || (ratio === bestRatio && ratio > 0 && pageNumber < bestPage)) {
          bestPage = pageNumber;
          bestRatio = ratio;
        }
      });
      if (bestPage > 0) setCurrentPage(bestPage);
    }, {
      root,
      rootMargin: '-8% 0px -42% 0px',
      threshold: [0, 0.1, 0.25, 0.5, 0.75, 1],
    });
    pages.forEach((page) => observer.observe(page));
    return () => observer.disconnect();
  }, [document]);

  useEffect(() => {
    const thumbnail = thumbnailScrollRef.current?.querySelector<HTMLElement>(
      `[data-page-number="${currentPage}"]`,
    );
    thumbnail?.scrollIntoView({
      block: 'nearest',
      inline: 'nearest',
      behavior: prefersReducedMotion() ? 'auto' : 'smooth',
    });
  }, [currentPage]);

  const selectPage = (pageNumber: number) => {
    setCurrentPage(pageNumber);
    const page = pageScrollRef.current?.querySelector<HTMLElement>(
      `[data-page-number="${pageNumber}"]`,
    );
    page?.scrollIntoView({
      block: 'start',
      behavior: prefersReducedMotion() ? 'auto' : 'smooth',
    });
  };

  if (failed) return <SlideDeckFallback src={src} title={title} />;
  if (!document) return <SlideDeckLoading />;

  return (
    <div className="slide-deck-preview" aria-label={`${title} 幻灯片预览`}>
      <div className="slide-deck-preview__layout">
        <aside className="slide-deck-preview__filmstrip" aria-label="幻灯片缩略图">
          <div className="slide-deck-preview__filmstrip-heading">
            <span className="slide-deck-preview__filmstrip-title">
              <Presentation size={15} aria-hidden="true" />
              幻灯片
            </span>
            <span>{document.numPages} 页</span>
          </div>
          <div ref={thumbnailScrollRef} className="slide-deck-preview__thumbnails">
            {pageNumbers.map((pageNumber) => (
              <LazySlideThumbnail
                key={pageNumber}
                active={currentPage === pageNumber}
                aspectRatio={aspectRatio}
                document={document}
                pageNumber={pageNumber}
                scrollRootRef={thumbnailScrollRef}
                onSelect={selectPage}
              />
            ))}
          </div>
        </aside>

        <section className="slide-deck-preview__stage" aria-label="幻灯片连续浏览">
          <div className="slide-deck-preview__stage-heading" aria-live="polite">
            <span className="slide-deck-preview__stage-title" title={title}>{title}</span>
            <span className="slide-deck-preview__page-position">
              <strong>{currentPage}</strong> / {document.numPages}
            </span>
          </div>
          <div ref={pageScrollRef} className="slide-deck-preview__pages">
            {pageNumbers.map((pageNumber) => {
              const shouldRender = Math.abs(pageNumber - currentPage) <= 2;
              return (
                <section
                  key={pageNumber}
                  className="slide-deck-preview__page"
                  data-slide-page
                  data-page-number={pageNumber}
                  data-rendered={shouldRender ? 'true' : 'false'}
                  aria-label={`第 ${pageNumber} 页，共 ${document.numPages} 页`}
                  style={{ aspectRatio: String(aspectRatio) }}
                >
                  {shouldRender ? (
                    <SlideCanvas document={document} pageNumber={pageNumber} variant="page" />
                  ) : (
                    <div className="slide-deck-preview__page-placeholder" aria-hidden="true">
                      <span>{pageNumber}</span>
                    </div>
                  )}
                </section>
              );
            })}
          </div>
        </section>
      </div>
    </div>
  );
}
