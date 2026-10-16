type PdfJs = typeof import('pdfjs-dist');
type PdfViewerModule = typeof import('pdfjs-dist/web/pdf_viewer.mjs');

let pdfJsPromise: Promise<PdfJs> | null = null;
let pdfViewerPromise: Promise<PdfViewerModule> | null = null;

export function loadPdfJs(): Promise<PdfJs> {
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

/**
 * pdf_viewer.mjs 在模块求值时读取 globalThis.pdfjsLib，必须在核心库加载后再导入。
 * 查看器样式随模块按需加载，不进入应用入口 CSS。
 */
export function loadPdfViewer(): Promise<PdfViewerModule> {
  if (!pdfViewerPromise) {
    pdfViewerPromise = loadPdfJs()
      .then(() => Promise.all([
        import('pdfjs-dist/web/pdf_viewer.mjs'),
        import('pdfjs-dist/web/pdf_viewer.css'),
      ]))
      .then(([viewerModule]) => viewerModule)
      .catch((error: unknown) => {
        pdfViewerPromise = null;
        throw error;
      });
  }
  return pdfViewerPromise;
}
