import { useEffect, useRef, useState, type TableHTMLAttributes } from 'react';
import { X, Download, AlertCircle, Code, Eye, Presentation, FileText, FileCode, FileImage, FileSpreadsheet, File, Archive, Folder } from 'lucide-react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { Prism as SyntaxHighlighter } from 'react-syntax-highlighter';
import { vscDarkPlus } from 'react-syntax-highlighter/dist/esm/styles/prism';
import mammoth from 'mammoth';
import DOMPurify from 'dompurify';
import { FileInfo } from '../types';
import { apiService } from '../services/api';
import { formatDownloadError } from '../utils/errorMessages';
import { buildSandboxFileUrl, getFileExtLabel, normalizeFileType } from '../utils/fileUtils';

interface FilePreviewProps {
  file: FileInfo | null;
  sessionId: string;
  onClose: () => void;
  previewUrlBuilder?: (file: FileInfo) => string;
  onDownloadFile?: (file: FileInfo) => Promise<void>;
  inline?: boolean;
}

type PreviewCacheEntry =
  | { kind: 'text'; text: string }
  | { kind: 'docx'; html: string }
  | { kind: 'csv'; rows: string[][] }
  | { kind: 'spreadsheet'; sheets: SpreadsheetSheet[] }
  | { kind: 'zip'; entries: ZipEntry[] }
  | { kind: 'binary'; blob: Blob };

interface SpreadsheetSheet {
  name: string;
  rows: string[][];
}

interface ZipEntry {
  path: string;
  directory: boolean;
}

const markdownComponents = {
  table: ({ children, ...props }: TableHTMLAttributes<HTMLTableElement>) => (
    <div className="markdown-table-scroll">
      <table {...props}>{children}</table>
    </div>
  ),
};

const PREVIEW_CACHE_LIMIT = 30;
const MAX_ZIP_PREVIEW_BYTES = 10 * 1024 * 1024;
const MAX_ZIP_ENTRIES = 2000;
const previewCache = new Map<string, PreviewCacheEntry>();

function readPreviewCache(key: string): PreviewCacheEntry | null {
  const cached = previewCache.get(key) || null;
  if (!cached) {
    return null;
  }
  // 读取即刷新，近似 LRU。
  previewCache.delete(key);
  previewCache.set(key, cached);
  return cached;
}

function writePreviewCache(key: string, value: PreviewCacheEntry) {
  previewCache.delete(key);
  previewCache.set(key, value);
  if (previewCache.size > PREVIEW_CACHE_LIMIT) {
    const oldestKey = previewCache.keys().next().value;
    if (oldestKey) {
      previewCache.delete(oldestKey);
    }
  }
}

export function FilePreview({ file, sessionId, onClose, previewUrlBuilder, onDownloadFile, inline = false }: FilePreviewProps) {
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [textContent, setTextContent] = useState('');
  const [docxHtml, setDocxHtml] = useState('');
  const [tableData, setTableData] = useState<string[][]>([]);
  const [spreadsheetSheets, setSpreadsheetSheets] = useState<SpreadsheetSheet[]>([]);
  const [zipEntries, setZipEntries] = useState<ZipEntry[]>([]);
  const [activeSheetIndex, setActiveSheetIndex] = useState(0);
  const [binaryPreviewUrl, setBinaryPreviewUrl] = useState('');
  const [htmlBlobUrl, setHtmlBlobUrl] = useState('');
  const [htmlFrameLoading, setHtmlFrameLoading] = useState(false);
  const [viewMode, setViewMode] = useState<'rendered' | 'source'>('rendered');
  const requestIdRef = useRef(0);
  const closeButtonRef = useRef<HTMLButtonElement>(null);

  const getPreviewApiUrl = () => {
    if (!file) return '';
    if (previewUrlBuilder) return previewUrlBuilder(file);
    return buildSandboxFileUrl(sessionId, file.path, true);
  };

  const getPreviewCacheKey = () => {
    if (!file) return '';
    return `${getPreviewApiUrl()}::${file.modified}::${file.size}`;
  };

  const setHtmlPreviewFromText = (text: string) => {
    const htmlBlob = new Blob([text], { type: 'text/html' });
    const objectUrl = URL.createObjectURL(htmlBlob);
    setHtmlBlobUrl((prev) => {
      if (prev) URL.revokeObjectURL(prev);
      return objectUrl;
    });
    setHtmlFrameLoading(true);
  };

  useEffect(() => {
    if (!file) {
      return;
    }

    requestIdRef.current += 1;
    const currentRequestId = requestIdRef.current;

    setError('');
    setTextContent('');
    setDocxHtml('');
    setTableData([]);
    setSpreadsheetSheets([]);
    setZipEntries([]);
    setActiveSheetIndex(0);
    setHtmlFrameLoading(false);
    setHtmlBlobUrl((prev) => {
      if (prev) URL.revokeObjectURL(prev);
      return '';
    });
    setBinaryPreviewUrl((prev) => {
      if (prev) URL.revokeObjectURL(prev);
      return '';
    });

    const fileType = normalizeFileType(file.name, file.type);
    if (isTextFile(fileType) || isMarkdownFile(fileType) || isHtmlFile(fileType) || isCodeFile(fileType)) {
      void loadTextContent(currentRequestId);
    } else if (isDocxFile(fileType)) {
      void loadDocxContent(currentRequestId);
    } else if (isCsvFile(fileType)) {
      void loadCsvContent(currentRequestId);
    } else if (isExcelFile(fileType)) {
      void loadSpreadsheetContent(currentRequestId);
    } else if (isZipFile(fileType)) {
      void loadZipContent(currentRequestId);
    } else if (isImageFile(fileType) || isPdfFile(fileType)) {
      void loadBinaryPreview(currentRequestId);
    }
  // Loader 函数有意使用本次 render 的 file/session 快照；requestIdRef 负责丢弃迟到响应。
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [file, sessionId, previewUrlBuilder]);

  useEffect(() => {
    return () => {
      if (binaryPreviewUrl) {
        URL.revokeObjectURL(binaryPreviewUrl);
      }
      if (htmlBlobUrl) {
        URL.revokeObjectURL(htmlBlobUrl);
      }
    };
  }, [binaryPreviewUrl, htmlBlobUrl]);

  useEffect(() => {
    if (inline || !file) return undefined;
    const previousFocus = document.activeElement instanceof HTMLElement
      ? document.activeElement
      : null;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    closeButtonRef.current?.focus({ preventScroll: true });
    return () => {
      document.body.style.overflow = previousOverflow;
      previousFocus?.focus({ preventScroll: true });
    };
  }, [file, inline]);

  const fetchPreviewResponse = async () => {
    const response = await fetch(getPreviewApiUrl(), {
      headers: {
        ...apiService.getAuthHeaders(),
      },
    });
    if (!response.ok) {
      throw new Error(`Failed to load file: ${response.status}`);
    }
    return response;
  };

  const loadTextContent = async (requestId: number) => {
    setLoading(true);
    setError('');
    try {
      const cacheKey = getPreviewCacheKey();
      const cached = cacheKey ? readPreviewCache(cacheKey) : null;
      if (cached && cached.kind === 'text') {
        if (requestId !== requestIdRef.current) return;
        setTextContent(cached.text);
        if (file && isHtmlFile(normalizeFileType(file.name, file.type))) {
          setHtmlPreviewFromText(cached.text);
        }
        return;
      }

      const response = await fetchPreviewResponse();
      const text = await response.text();
      if (requestId !== requestIdRef.current) return;
      setTextContent(text);
      if (cacheKey) {
        writePreviewCache(cacheKey, { kind: 'text', text });
      }
      if (file && isHtmlFile(normalizeFileType(file.name, file.type))) {
        setHtmlPreviewFromText(text);
      }
    } catch (err) {
      if (requestId !== requestIdRef.current) return;
      console.error('Failed to load text content:', err);
      setError('加载文件内容失败');
    } finally {
      if (requestId === requestIdRef.current) {
        setLoading(false);
      }
    }
  };

  const loadDocxContent = async (requestId: number) => {
    setLoading(true);
    setError('');
    try {
      const cacheKey = getPreviewCacheKey();
      const cached = cacheKey ? readPreviewCache(cacheKey) : null;
      if (cached && cached.kind === 'docx') {
        if (requestId !== requestIdRef.current) return;
        setDocxHtml(cached.html);
        return;
      }

      const response = await fetchPreviewResponse();
      const arrayBuffer = await response.arrayBuffer();
      const result = await mammoth.convertToHtml({ arrayBuffer });
      if (requestId !== requestIdRef.current) return;
      const sanitizedHtml = DOMPurify.sanitize(result.value, { USE_PROFILES: { html: true } });
      setDocxHtml(sanitizedHtml);
      if (cacheKey) {
        writePreviewCache(cacheKey, { kind: 'docx', html: sanitizedHtml });
      }
      if (result.messages.length > 0) {
        console.warn('DOCX conversion warnings:', result.messages);
      }
    } catch (err) {
      if (requestId !== requestIdRef.current) return;
      console.error('Failed to load DOCX content:', err);
      setError('加载 DOCX 文件失败');
    } finally {
      if (requestId === requestIdRef.current) {
        setLoading(false);
      }
    }
  };

  const loadCsvContent = async (requestId: number) => {
    setLoading(true);
    setError('');
    try {
      const cacheKey = getPreviewCacheKey();
      const cached = cacheKey ? readPreviewCache(cacheKey) : null;
      if (cached && cached.kind === 'csv') {
        if (requestId !== requestIdRef.current) return;
        setTableData(cached.rows);
        return;
      }

      const response = await fetchPreviewResponse();
      const text = await response.text();
      const rows = text
        .split(/\r?\n/)
        .filter((line) => line.length > 0)
        .map((line) => line.split(','));
      if (requestId !== requestIdRef.current) return;
      setTableData(rows);
      if (cacheKey) {
        writePreviewCache(cacheKey, { kind: 'csv', rows });
      }
    } catch (err) {
      if (requestId !== requestIdRef.current) return;
      console.error('Failed to load CSV content:', err);
      setError('加载 CSV 文件失败');
    } finally {
      if (requestId === requestIdRef.current) {
        setLoading(false);
      }
    }
  };

  const loadSpreadsheetContent = async (requestId: number) => {
    setLoading(true);
    setError('');
    try {
      const cacheKey = getPreviewCacheKey();
      const cached = cacheKey ? readPreviewCache(cacheKey) : null;
      if (cached && cached.kind === 'spreadsheet') {
        if (requestId !== requestIdRef.current) return;
        setSpreadsheetSheets(cached.sheets);
        setActiveSheetIndex(0);
        return;
      }

      const response = await fetchPreviewResponse();
      const arrayBuffer = await response.arrayBuffer();
      const XLSX = await import('xlsx');
      const workbook = XLSX.read(arrayBuffer, { type: 'array', cellDates: true });
      const sheets = workbook.SheetNames.map((name) => {
        const worksheet = workbook.Sheets[name];
        const rawRows = XLSX.utils.sheet_to_json<unknown[]>(worksheet, {
          header: 1,
          raw: false,
          defval: '',
          blankrows: false,
        }) as unknown[][];
        const rows = rawRows
          .map((row) => row.map(formatSpreadsheetCell))
          .filter((row) => row.some((cell) => cell.length > 0));
        return { name, rows };
      });
      if (requestId !== requestIdRef.current) return;
      setSpreadsheetSheets(sheets);
      setActiveSheetIndex(0);
      if (cacheKey) {
        writePreviewCache(cacheKey, { kind: 'spreadsheet', sheets });
      }
    } catch (err) {
      if (requestId !== requestIdRef.current) return;
      console.error('Failed to load spreadsheet content:', err);
      setError('加载电子表格失败');
    } finally {
      if (requestId === requestIdRef.current) {
        setLoading(false);
      }
    }
  };

  const loadZipContent = async (requestId: number) => {
    setLoading(true);
    setError('');
    try {
      if (file && file.size > MAX_ZIP_PREVIEW_BYTES) {
        throw new Error('ZIP 文件超过 10 MB，请下载后解压查看');
      }
      const cacheKey = getPreviewCacheKey();
      const cached = cacheKey ? readPreviewCache(cacheKey) : null;
      if (cached?.kind === 'zip') {
        if (requestId !== requestIdRef.current) return;
        setZipEntries(cached.entries);
        return;
      }
      const response = await fetchPreviewResponse();
      const arrayBuffer = await response.arrayBuffer();
      if (arrayBuffer.byteLength > MAX_ZIP_PREVIEW_BYTES) {
        throw new Error('ZIP 文件超过 10 MB，请下载后解压查看');
      }
      const JSZip = (await import('jszip')).default;
      const archive = await JSZip.loadAsync(arrayBuffer, { createFolders: true });
      const rawEntries = Object.values(archive.files);
      if (rawEntries.length > MAX_ZIP_ENTRIES) {
        throw new Error(`ZIP 目录超过 ${MAX_ZIP_ENTRIES} 项，请下载后解压查看`);
      }
      const entries = rawEntries
        .filter((entry) => {
          const segments = entry.name.replace(/\\/g, '/').split('/').filter(Boolean);
          return segments.length > 0
            && !segments.some((segment) => segment === '..' || segment === '.');
        })
        .map((entry) => ({ path: entry.name, directory: entry.dir }))
        .sort((a, b) => Number(b.directory) - Number(a.directory) || a.path.localeCompare(b.path));
      if (requestId !== requestIdRef.current) return;
      setZipEntries(entries);
      if (cacheKey) writePreviewCache(cacheKey, { kind: 'zip', entries });
    } catch (err) {
      if (requestId !== requestIdRef.current) return;
      console.error('Failed to load ZIP directory:', err);
      setError(err instanceof Error ? err.message : '加载 ZIP 目录失败');
    } finally {
      if (requestId === requestIdRef.current) setLoading(false);
    }
  };

  const loadBinaryPreview = async (requestId: number) => {
    setLoading(true);
    setError('');
    try {
      const cacheKey = getPreviewCacheKey();
      const cached = cacheKey ? readPreviewCache(cacheKey) : null;
      if (cached && cached.kind === 'binary') {
        if (requestId !== requestIdRef.current) return;
        const cachedObjectUrl = URL.createObjectURL(cached.blob);
        setBinaryPreviewUrl((prev) => {
          if (prev) URL.revokeObjectURL(prev);
          return cachedObjectUrl;
        });
        return;
      }

      const response = await fetchPreviewResponse();
      const blob = await response.blob();
      if (requestId !== requestIdRef.current) return;
      const objectUrl = URL.createObjectURL(blob);
      setBinaryPreviewUrl((prev) => {
        if (prev) URL.revokeObjectURL(prev);
        return objectUrl;
      });
      if (cacheKey) {
        writePreviewCache(cacheKey, { kind: 'binary', blob });
      }
    } catch (err) {
      if (requestId !== requestIdRef.current) return;
      console.error('Failed to load binary content:', err);
      setError('加载文件预览失败');
    } finally {
      if (requestId === requestIdRef.current) {
        setLoading(false);
      }
    }
  };

  const isImageFile = (type: string) => ['jpg', 'jpeg', 'png', 'gif', 'svg', 'webp', 'bmp'].includes(type.toLowerCase());
  const isPdfFile = (type: string) => type.toLowerCase() === 'pdf';
  const isMarkdownFile = (type: string) => ['md', 'markdown'].includes(type.toLowerCase());
  const isHtmlFile = (type: string) => ['html', 'htm'].includes(type.toLowerCase());
  const isCodeFile = (type: string) => {
    const codeTypes = ['js', 'ts', 'jsx', 'tsx', 'py', 'java', 'cpp', 'c', 'go', 'rs', 'sh', 'bash', 'sql', 'css', 'json', 'xml', 'yaml', 'yml', 'rb', 'php', 'swift', 'kt', 'scala', 'r', 'dart', 'lua'];
    return codeTypes.includes(type.toLowerCase());
  };
  const isTextFile = (type: string) => ['txt', 'log', 'ini', 'conf', 'cfg', 'toml'].includes(type.toLowerCase());
  const isDocxFile = (type: string) => ['docx', 'doc'].includes(type.toLowerCase());
  const isCsvFile = (type: string) => type.toLowerCase() === 'csv';
  const isExcelFile = (type: string) => ['xlsx', 'xls'].includes(type.toLowerCase());
  const isSpreadsheetFile = (type: string) => ['xlsx', 'xls', 'csv'].includes(type.toLowerCase());
  const isPptxFile = (type: string) => ['pptx', 'ppt'].includes(type.toLowerCase());
  const isZipFile = (type: string) => type.toLowerCase() === 'zip';

  const getLanguage = (type: string): string => {
    const langMap: Record<string, string> = {
      'js': 'javascript', 'ts': 'typescript', 'jsx': 'jsx', 'tsx': 'tsx', 'py': 'python',
      'rb': 'ruby', 'sh': 'bash', 'yml': 'yaml', 'rs': 'rust', 'cpp': 'cpp', 'java': 'java',
      'go': 'go', 'sql': 'sql', 'json': 'json', 'xml': 'xml', 'html': 'html', 'css': 'css',
      'php': 'php', 'swift': 'swift', 'kt': 'kotlin', 'scala': 'scala',
    };
    return langMap[type.toLowerCase()] || type.toLowerCase();
  };

  const getFileIcon = (type: string, size: number = 20) => {
    if (isImageFile(type)) return <FileImage size={size} />;
    if (isSpreadsheetFile(type)) return <FileSpreadsheet size={size} />;
    if (isCodeFile(type) || isHtmlFile(type)) return <FileCode size={size} />;
    if (isTextFile(type) || isDocxFile(type) || isPdfFile(type) || isMarkdownFile(type)) return <FileText size={size} />;
    if (isPptxFile(type)) return <Presentation size={size} />;
    if (isZipFile(type)) return <Archive size={size} />;
    return <File size={size} />;
  };

  const handleDownload = async () => {
    if (!file) return;
    try {
      if (onDownloadFile) {
        await onDownloadFile(file);
      } else {
        await apiService.downloadFile(sessionId, file.path);
      }
    } catch (err) {
      console.error('Failed to download file:', err);
      setError(formatDownloadError(err));
    }
  };

  if (!file) return null;
  const fileType = normalizeFileType(file.name, file.type);

  const cardClassName = inline
    ? 'h-full flex flex-col bg-white min-w-0'
    : 'relative w-full max-w-[960px] h-full bg-white rounded-[32px] shadow-2xl flex flex-col overflow-hidden animate-zoom-in';

  const headerClassName = inline
    ? 'h-14 border-b border-black/[0.06] flex items-center justify-between px-4 shrink-0 bg-white'
    : 'h-16 border-b border-black/[0.06] flex items-center justify-between px-6 shrink-0 bg-white/80 backdrop-blur-xl';

  const contentClassName = inline
    ? 'flex-1 min-w-0 overflow-auto p-5 bg-claude-bg'
    : 'flex-1 min-w-0 overflow-auto p-8 bg-claude-bg';

  const closeButtonClassName = inline
    ? 'p-2.5 hover:bg-claude-hover rounded-full transition-colors text-claude-muted'
    : 'p-2.5 bg-black text-white rounded-full hover:opacity-80 transition-[opacity,transform] active:scale-90 shadow-lg';

  const activeSpreadsheetSheet = spreadsheetSheets[activeSheetIndex];

  const renderTablePreview = (rows: string[][]) => (
    <div className="bg-white rounded-2xl shadow-sm border border-black/[0.03] overflow-hidden">
      {rows.length === 0 ? (
        <div className="py-16 text-center text-[13px] text-claude-muted">空表格</div>
      ) : (
        <div className="overflow-auto p-4 max-h-[60vh]">
          <table className="min-w-full border-collapse">
            <tbody>
              {rows.map((row, rowIndex) => (
                <tr key={rowIndex} className={rowIndex === 0 ? 'bg-claude-surface font-medium' : ''}>
                  {row.map((cell, cellIndex) => (
                    <td key={cellIndex} className="border border-claude-border px-4 py-2 text-[13px] text-claude-text whitespace-pre-wrap align-top">
                      {cell}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );

  const previewCard = (
    <div
      className={cardClassName}
      onClick={inline ? undefined : (e) => e.stopPropagation()}
      data-testid={inline ? 'file-preview-inline' : undefined}
    >
      {/* Header */}
      <div className={headerClassName}
      >
        <div className="flex items-center space-x-4">
          <div className="w-10 h-10 bg-claude-surface rounded-xl flex items-center justify-center text-claude-accent">
            {getFileIcon(fileType, 20)}
          </div>
          <div>
            <h2 className="text-[15px] font-semibold tracking-tight text-claude-text">{file.name}</h2>
            <p className="text-[11px] text-claude-muted uppercase tracking-wider">
              {getFileExtLabel(file)} · {formatFileSize(file.size)}
            </p>
          </div>
        </div>
        <div className="flex items-center space-x-2">
          {/* HTML/MD 视图切换 */}
          {(isHtmlFile(fileType) || isMarkdownFile(fileType)) && (
            <div className="flex bg-claude-surface rounded-xl p-1 mr-2">
              <button
                onClick={() => setViewMode('rendered')}
                className={`p-2 rounded-lg transition-[color,background-color,box-shadow] ${
                  viewMode === 'rendered' ? 'bg-white text-claude-text shadow-sm' : 'text-claude-muted'
                }`}
                title="渲染视图"
              >
                <Eye size={16} />
              </button>
              <button
                onClick={() => setViewMode('source')}
                className={`p-2 rounded-lg transition-[color,background-color,box-shadow] ${
                  viewMode === 'source' ? 'bg-white text-claude-text shadow-sm' : 'text-claude-muted'
                }`}
                title="源代码"
              >
                <Code size={16} />
              </button>
            </div>
          )}
          <button
            onClick={handleDownload}
            className="p-2.5 hover:bg-claude-hover rounded-full transition-colors text-claude-muted"
            title="下载文件"
          >
            <Download size={18} />
          </button>
          <button
            ref={closeButtonRef}
            onClick={onClose}
            className={closeButtonClassName}
            title={inline ? '关闭预览' : '关闭'}
          >
            <X size={18} />
          </button>
        </div>
      </div>

      {/* Content */}
      <div className={contentClassName}>
        {error ? (
          <div className="flex items-center gap-3 p-4 bg-red-50 border border-red-100 rounded-2xl">
            <AlertCircle className="w-5 h-5 text-claude-error" />
            <span className="text-claude-error font-medium">{error}</span>
          </div>
        ) : loading ? (
          <div className="flex items-center justify-center py-24" data-testid="file-preview-loading">
            <div className="flex space-x-2">
              {[0, 1, 2].map(i => (
                <div
                  key={i}
                  className="w-2 h-2 bg-claude-accent rounded-full animate-dot-pulse"
                  style={{ animationDelay: `${i * 200}ms` }}
                />
              ))}
            </div>
          </div>
        ) : isImageFile(fileType) ? (
          <div className="h-full flex items-center justify-center">
            <div className="w-full max-w-2xl bg-white border border-black/[0.05] rounded-3xl shadow-xl p-4">
              <img
                src={binaryPreviewUrl}
                alt={file.name}
                className="max-w-full max-h-[60vh] object-contain mx-auto rounded-2xl"
                onError={() => setError('图片加载失败')}
              />
            </div>
          </div>
        ) : isPdfFile(fileType) ? (
          <div className="w-full h-full bg-white rounded-2xl overflow-hidden shadow-lg">
            <iframe
              src={binaryPreviewUrl}
              className="w-full h-full border-0"
              title={file.name}
              onError={() => setError('PDF 加载失败')}
            />
          </div>
        ) : isMarkdownFile(fileType) ? (
          viewMode === 'rendered' ? (
            <div className="w-full max-w-[860px] mx-auto bg-white p-8 sm:p-12 rounded-2xl shadow-sm border border-black/[0.03]">
              <div className="prose max-w-none min-w-0">
                <ReactMarkdown remarkPlugins={[remarkGfm]} components={markdownComponents}>{textContent}</ReactMarkdown>
              </div>
            </div>
          ) : (
            <div className="bg-[#1C1C1E] rounded-2xl shadow-2xl ring-1 ring-white/10 overflow-hidden">
              <SyntaxHighlighter
                language="markdown"
                style={vscDarkPlus}
                showLineNumbers
                customStyle={{ margin: 0, borderRadius: 0, fontSize: '13px', background: 'transparent' }}
              >
                {textContent}
              </SyntaxHighlighter>
            </div>
          )
        ) : isHtmlFile(fileType) ? (
          viewMode === 'rendered' ? (
            <div className="relative w-full h-full bg-white rounded-2xl overflow-hidden shadow-lg">
              {htmlFrameLoading && (
                <div className="absolute inset-0 z-10 flex items-center justify-center bg-white/90" data-testid="html-iframe-loading">
                  <div className="flex space-x-2">
                    {[0, 1, 2].map(i => (
                      <div
                        key={i}
                        className="w-2 h-2 bg-claude-accent rounded-full animate-dot-pulse"
                        style={{ animationDelay: `${i * 200}ms` }}
                      />
                    ))}
                  </div>
                </div>
              )}
              <iframe
                src={htmlBlobUrl}
                className="w-full h-full border-0"
                title={file.name}
                sandbox="allow-scripts"
                onLoad={() => setHtmlFrameLoading(false)}
                onError={() => {
                  setHtmlFrameLoading(false);
                  setError('HTML 加载失败');
                }}
              />
            </div>
          ) : (
            <div className="bg-[#1C1C1E] rounded-2xl shadow-2xl ring-1 ring-white/10 overflow-hidden">
              <SyntaxHighlighter language="html" style={vscDarkPlus} showLineNumbers customStyle={{ margin: 0, borderRadius: 0, fontSize: '13px', background: 'transparent' }}>
                {textContent}
              </SyntaxHighlighter>
            </div>
          )
        ) : isCodeFile(fileType) ? (
          <div className="bg-[#1C1C1E] rounded-2xl shadow-2xl ring-1 ring-white/10 overflow-hidden">
            <SyntaxHighlighter language={getLanguage(fileType)} style={vscDarkPlus} showLineNumbers customStyle={{ margin: 0, borderRadius: 0, fontSize: '13px', background: 'transparent' }}>
              {textContent}
            </SyntaxHighlighter>
          </div>
        ) : isTextFile(fileType) ? (
          <div className="max-w-2xl mx-auto bg-white p-8 rounded-2xl shadow-sm border border-black/[0.03]">
            <pre className="text-[13px] font-mono text-claude-text whitespace-pre-wrap break-words">{textContent}</pre>
          </div>
        ) : isDocxFile(fileType) ? (
          <div className="max-w-2xl mx-auto bg-white p-12 rounded-2xl shadow-sm border border-black/[0.03]">
            <div className="prose max-w-none" dangerouslySetInnerHTML={{ __html: docxHtml }} />
          </div>
        ) : isSpreadsheetFile(fileType) ? (
          isCsvFile(fileType) ? (
            renderTablePreview(tableData)
          ) : (
            <div className="space-y-3">
              {spreadsheetSheets.length > 1 && (
                <div className="flex gap-1 overflow-x-auto rounded-xl bg-claude-surface p-1" role="tablist" aria-label="工作表">
                  {spreadsheetSheets.map((sheet, index) => (
                    <button
                      key={`${sheet.name}-${index}`}
                      type="button"
                      role="tab"
                      aria-selected={activeSheetIndex === index}
                      onClick={() => setActiveSheetIndex(index)}
                      className={`shrink-0 rounded-lg px-3 py-1.5 text-[12px] font-medium transition-colors ${
                        activeSheetIndex === index
                          ? 'bg-white text-claude-text shadow-sm'
                          : 'text-claude-muted hover:text-claude-text'
                      }`}
                    >
                      {sheet.name}
                    </button>
                  ))}
                </div>
              )}
              {renderTablePreview(activeSpreadsheetSheet?.rows || [])}
            </div>
          )
        ) : isZipFile(fileType) ? (
          <div className="mx-auto w-full max-w-3xl overflow-hidden rounded-2xl border border-black/[0.06] bg-white shadow-sm">
            <div className="flex items-center justify-between border-b border-claude-border px-4 py-3">
              <div className="flex items-center gap-2 text-sm font-semibold text-claude-text">
                <Archive size={17} className="text-claude-accent" />
                压缩包目录
              </div>
              <span className="text-xs text-claude-muted">{zipEntries.length} 项（只读）</span>
            </div>
            {zipEntries.length === 0 ? (
              <div className="py-16 text-center text-sm text-claude-muted">压缩包为空</div>
            ) : (
              <ul className="max-h-[65vh] overflow-y-auto divide-y divide-claude-border/60">
                {zipEntries.map((entry) => (
                  <li key={`${entry.directory ? 'd' : 'f'}:${entry.path}`} className="flex items-center gap-3 px-4 py-2.5">
                    {entry.directory
                      ? <Folder size={15} className="shrink-0 text-claude-accent" />
                      : <File size={15} className="shrink-0 text-claude-muted" />}
                    <span className="min-w-0 break-all font-mono text-xs text-claude-text">
                      {entry.path}
                    </span>
                  </li>
                ))}
              </ul>
            )}
          </div>
        ) : isPptxFile(fileType) ? (
          <div className="flex flex-col items-center justify-center py-12 px-6">
            <div className="mb-6 p-6 bg-[#FF9500]/10 rounded-full">
              <Presentation className="w-16 h-16 text-[#FF9500]" />
            </div>
            <div className="bg-white border border-black/[0.05] rounded-2xl p-6 max-w-md w-full mb-6">
              <h4 className="text-[16px] font-semibold text-claude-text mb-2">演示文稿文件</h4>
              <p className="text-[13px] text-claude-muted mb-4">
                PowerPoint 演示文稿暂不支持在线预览，请下载后使用 Microsoft PowerPoint、WPS 或其他兼容软件打开。
              </p>
              <div className="space-y-2 text-[13px]">
                <div className="flex justify-between items-center py-2 border-b border-claude-border">
                  <span className="text-claude-muted">文件名：</span>
                  <span className="font-medium text-claude-text truncate ml-2">{file.name}</span>
                </div>
                <div className="flex justify-between items-center py-2 border-b border-claude-border">
                  <span className="text-claude-muted">文件类型：</span>
                  <span className="font-medium text-claude-text">{getFileExtLabel(file)}</span>
                </div>
                <div className="flex justify-between items-center py-2">
                  <span className="text-claude-muted">文件大小：</span>
                  <span className="font-medium text-claude-text">{formatFileSize(file.size)}</span>
                </div>
              </div>
            </div>
            <div className="flex flex-col sm:flex-row gap-3 w-full max-w-md">
              <button
                onClick={handleDownload}
                className="flex-1 inline-flex items-center justify-center gap-2 px-6 py-3 bg-black hover:bg-black/80 text-white rounded-xl transition-colors font-medium"
              >
                <Download className="w-5 h-5" />
                下载文件
              </button>
            </div>
          </div>
        ) : (
          <div className="text-center py-24">
            <AlertCircle className="w-12 h-12 mx-auto text-claude-border mb-3" />
            <p className="text-claude-muted mb-4">此文件类型不支持预览</p>
            <button
              onClick={handleDownload}
              className="inline-flex items-center gap-2 px-6 py-3 bg-black hover:bg-black/80 text-white rounded-xl transition-colors font-medium"
            >
              <Download className="w-4 h-4" />
              下载文件
            </button>
          </div>
        )}
      </div>
    </div>
  );

  if (inline) {
    return previewCard;
  }

  return (
    <div
      className="fixed inset-0 z-[100] flex items-center justify-center p-6 sm:p-12 animate-fade-in"
      onClick={onClose}
    >
      {/* 背景遮罩 */}
      <div className="absolute inset-0 bg-black/40 backdrop-blur-md" />

      {/* 预览卡片 */}
      {previewCard}
    </div>
  );
}

function formatFileSize(bytes: number): string {
  if (bytes < 1024) return bytes + ' B';
  if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB';
  return (bytes / (1024 * 1024)).toFixed(1) + ' MB';
}

function formatSpreadsheetCell(value: unknown): string {
  if (value instanceof Date) {
    return value.toLocaleString();
  }
  return String(value);
}
