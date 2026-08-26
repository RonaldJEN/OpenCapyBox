import {
  forwardRef,
  lazy,
  Suspense,
  useCallback,
  useEffect,
  useImperativeHandle,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from 'react';
import {
  AlertCircle,
  Archive,
  Code,
  Download,
  ExternalLink,
  Eye,
  File,
  FileCode,
  FileImage,
  FileSpreadsheet,
  FileText,
  Folder,
  MoreHorizontal,
  PanelLeftClose,
  PanelLeftOpen,
  Presentation,
  RotateCcw,
  Search,
  Save,
  WrapText,
  X,
  ZoomIn,
  ZoomOut,
} from 'lucide-react';
import { Prism as SyntaxHighlighter } from 'react-syntax-highlighter';
import { vscDarkPlus } from 'react-syntax-highlighter/dist/esm/styles/prism';
import mammoth from 'mammoth';
import DOMPurify from 'dompurify';
import type { FileInfo } from '../types';
import { apiService } from '../services/api';
import { formatDownloadError } from '../utils/errorMessages';
import { buildSandboxFileUrl, getFileExtLabel } from '../utils/fileUtils';
import { extractMarkdownHeadings, MarkdownReportPreview } from './file-preview/MarkdownReportPreview';
import { SlideDeckPreview } from './file-preview/SlideDeckPreview';
import { openHtmlBlobPreview } from './file-preview/htmlBlobPreview';
import {
  resolvePreviewDescriptor,
  withRenderFormat,
  type PreviewDescriptor,
} from './file-preview/previewRegistry';
import './file-preview/filePreview.css';
import type { VditorMarkdownEditorHandle } from './file-preview/VditorMarkdownEditor';
import type { SpreadsheetEditorHandle } from './file-preview/SpreadsheetEditor';

interface FilePreviewProps {
  file: FileInfo | null;
  sessionId: string;
  ownerEpoch?: number;
  onClose: () => void;
  readOnly?: boolean;
  previewUrlBuilder?: (file: FileInfo) => string;
  onDownloadFile?: (file: FileInfo) => Promise<void>;
  onFileUpdated?: (file: FileInfo) => void;
  onDirtyChange?: (dirty: boolean) => void;
  onSavingChange?: (saving: boolean) => void;
  onSaveFailure?: () => void;
  saveRequestNonce?: number;
  inline?: boolean;
}

export interface SessionFileOwnerIdentity {
  ownerSessionId: string;
  ownerEpoch: number;
}

export interface FilePreviewSaveResult extends SessionFileOwnerIdentity {
  path: string;
  ok: boolean;
  stale: boolean;
}

export interface FilePreviewHandle extends SessionFileOwnerIdentity {
  path: string;
  isDirty: (expectedOwner: SessionFileOwnerIdentity) => boolean;
  saveDirty: (expectedOwner: SessionFileOwnerIdentity) => Promise<FilePreviewSaveResult>;
}

type PreviewViewMode = 'rendered' | 'source';

export const MARKDOWN_AUTOSAVE_DELAY_MS = 900;

const VditorMarkdownEditor = lazy(async () => {
  const module = await import('./file-preview/VditorMarkdownEditor');
  return { default: module.VditorMarkdownEditor };
});

const SpreadsheetEditor = lazy(async () => {
  const module = await import('./file-preview/SpreadsheetEditor');
  return { default: module.SpreadsheetEditor };
});

type PreviewCacheEntry =
  | { kind: 'text'; text: string }
  | { kind: 'docx'; html: string }
  | { kind: 'zip'; entries: ZipEntry[] }
  | { kind: 'binary'; blob: Blob };

interface ZipEntry {
  path: string;
  directory: boolean;
}

const PREVIEW_CACHE_LIMIT = 30;
const MAX_ZIP_PREVIEW_BYTES = 10 * 1024 * 1024;
const MAX_ZIP_ENTRIES = 2000;
const previewCache = new Map<string, PreviewCacheEntry>();

function readPreviewCache(key: string): PreviewCacheEntry | null {
  const cached = previewCache.get(key) || null;
  if (!cached) return null;
  previewCache.delete(key);
  previewCache.set(key, cached);
  return cached;
}

function writePreviewCache(key: string, value: PreviewCacheEntry) {
  previewCache.delete(key);
  previewCache.set(key, value);
  if (previewCache.size > PREVIEW_CACHE_LIMIT) {
    const oldestKey = previewCache.keys().next().value;
    if (oldestKey) previewCache.delete(oldestKey);
  }
}

function isAbortError(error: unknown): boolean {
  return error instanceof DOMException && error.name === 'AbortError';
}

export const FilePreview = forwardRef<FilePreviewHandle, FilePreviewProps>(function FilePreview({
  file,
  sessionId,
  ownerEpoch = 0,
  onClose,
  readOnly = false,
  previewUrlBuilder,
  onDownloadFile,
  onFileUpdated,
  onDirtyChange,
  onSavingChange,
  onSaveFailure,
  saveRequestNonce,
  inline = false,
}: FilePreviewProps, ref) {
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [previewNotice, setPreviewNotice] = useState('');
  const [textContent, setTextContent] = useState('');
  const [markdownDraft, setMarkdownDraft] = useState('');
  const [fileVersion, setFileVersion] = useState<FileInfo | null>(file);
  const [savingMarkdown, setSavingMarkdown] = useState(false);
  const [markdownSaveMessage, setMarkdownSaveMessage] = useState('');
  const [markdownSaveError, setMarkdownSaveError] = useState('');
  const [markdownRevision, setMarkdownRevision] = useState(0);
  const [markdownToolbarOpen, setMarkdownToolbarOpen] = useState(false);
  const [markdownTocOpen, setMarkdownTocOpen] = useState(false);
  const [docxHtml, setDocxHtml] = useState('');
  const [spreadsheetSource, setSpreadsheetSource] = useState<ArrayBuffer | null>(null);
  const [spreadsheetDirty, setSpreadsheetDirty] = useState(false);
  const [spreadsheetRevision, setSpreadsheetRevision] = useState(0);
  const [savingSpreadsheet, setSavingSpreadsheet] = useState(false);
  const [spreadsheetSaveMessage, setSpreadsheetSaveMessage] = useState('');
  const [spreadsheetSaveError, setSpreadsheetSaveError] = useState('');
  const [zipEntries, setZipEntries] = useState<ZipEntry[]>([]);
  const [zipQuery, setZipQuery] = useState('');
  const [binaryPreviewUrl, setBinaryPreviewUrl] = useState('');
  const [convertedPdfUrl, setConvertedPdfUrl] = useState('');
  const [htmlFrameLoading, setHtmlFrameLoading] = useState(false);
  const [viewMode, setViewMode] = useState<PreviewViewMode>('rendered');
  const [wrapLongLines, setWrapLongLines] = useState(false);
  const [imageScale, setImageScale] = useState(1);
  const [imageRotation, setImageRotation] = useState(0);
  const requestIdRef = useRef(0);
  const closeButtonRef = useRef<HTMLButtonElement>(null);
  const contentViewportRef = useRef<HTMLDivElement>(null);
  const markdownEditorRef = useRef<VditorMarkdownEditorHandle>(null);
  const markdownRevisionRef = useRef(0);
  const spreadsheetEditorRef = useRef<SpreadsheetEditorHandle>(null);
  const spreadsheetRevisionRef = useRef(0);
  const selfSavedModifiedRef = useRef('');
  const loadedFileIdentityRef = useRef('');
  const handledSaveRequestNonceRef = useRef<number | undefined>(undefined);
  const markdownSavePromiseRef = useRef<Promise<boolean> | null>(null);
  const spreadsheetSavePromiseRef = useRef<Promise<boolean> | null>(null);
  const ownerIdentityRef = useRef<SessionFileOwnerIdentity>({ ownerSessionId: sessionId, ownerEpoch });
  const fileVersionRef = useRef(fileVersion);
  const markdownDraftRef = useRef(markdownDraft);
  const textContentRef = useRef(textContent);
  const spreadsheetDirtyRef = useRef(spreadsheetDirty);
  ownerIdentityRef.current = { ownerSessionId: sessionId, ownerEpoch };
  fileVersionRef.current = fileVersion;
  markdownDraftRef.current = markdownDraft;
  textContentRef.current = textContent;
  spreadsheetDirtyRef.current = spreadsheetDirty;
  const fileRef = useRef(file);
  fileRef.current = file;
  const previewUrlBuilderRef = useRef(previewUrlBuilder);
  previewUrlBuilderRef.current = previewUrlBuilder;
  const onDirtyChangeRef = useRef(onDirtyChange);
  onDirtyChangeRef.current = onDirtyChange;
  const onSavingChangeRef = useRef(onSavingChange);
  onSavingChangeRef.current = onSavingChange;
  const onSaveFailureRef = useRef(onSaveFailure);
  onSaveFailureRef.current = onSaveFailure;

  const descriptor = useMemo(
    () => file ? resolvePreviewDescriptor(file) : null,
    [file],
  );
  const effectiveReadOnly = readOnly || Boolean(previewUrlBuilder);
  const spreadsheetReadOnly = descriptor?.kind === 'spreadsheet' && (
    effectiveReadOnly || descriptor.type === 'xls' || descriptor.type === 'et'
  );
  const markdownHeadings = useMemo(
    () => descriptor?.kind === 'markdown' ? extractMarkdownHeadings(markdownDraft) : [],
    [descriptor?.kind, markdownDraft],
  );

  const getPreviewApiUrl = () => {
    if (!file) return '';
    if (previewUrlBuilder) return previewUrlBuilder(file);
    return buildSandboxFileUrl(sessionId, file.path, true);
  };

  const getPreviewCacheKey = (suffix = '') => {
    if (!file) return '';
    return `${getPreviewApiUrl()}::${file.modified}::${file.size}${suffix}`;
  };

  const buildMarkdownSessionFileUrl = useCallback((resolvedPath: string) => {
    const currentFile = fileRef.current;
    if (!currentFile) return '';
    const resolvedFile = {
      ...currentFile,
      path: resolvedPath,
      name: resolvedPath.split('/').pop() || resolvedPath,
    };
    const currentPreviewUrlBuilder = previewUrlBuilderRef.current;
    return currentPreviewUrlBuilder
      ? currentPreviewUrlBuilder(resolvedFile)
      : buildSandboxFileUrl(sessionId, resolvedPath, true);
  }, [sessionId]);

  useLayoutEffect(() => {
    if (!file) return undefined;

    const fileIdentity = `${sessionId}::${file.path}`;
    if (
      loadedFileIdentityRef.current === fileIdentity
      && selfSavedModifiedRef.current
      && selfSavedModifiedRef.current === file.modified
    ) {
      selfSavedModifiedRef.current = '';
      setFileVersion(file);
      return undefined;
    }
    loadedFileIdentityRef.current = fileIdentity;

    requestIdRef.current += 1;
    const requestId = requestIdRef.current;
    const controller = new AbortController();
    const nextDescriptor = resolvePreviewDescriptor(file);

    setError('');
    setPreviewNotice('');
    setTextContent('');
    setMarkdownDraft('');
    setFileVersion(file);
    setSavingMarkdown(false);
    setMarkdownSaveMessage('');
    setMarkdownSaveError('');
    markdownRevisionRef.current = 0;
    setMarkdownRevision(0);
    setMarkdownToolbarOpen(false);
    setMarkdownTocOpen(false);
    setDocxHtml('');
    setSpreadsheetSource(null);
    setSpreadsheetDirty(false);
    spreadsheetRevisionRef.current = 0;
    setSpreadsheetRevision(0);
    setSavingSpreadsheet(false);
    setSpreadsheetSaveMessage('');
    setSpreadsheetSaveError('');
    setZipEntries([]);
    setZipQuery('');
    setBinaryPreviewUrl('');
    setConvertedPdfUrl('');
    setHtmlFrameLoading(false);
    setViewMode('rendered');
    setWrapLongLines(false);
    setImageScale(1);
    setImageRotation(0);

    switch (nextDescriptor.kind) {
      case 'text':
      case 'markdown':
      case 'html':
      case 'code':
        void loadTextContent(requestId, controller.signal);
        break;
      case 'document':
      case 'presentation':
        void loadOfficePreview(requestId, controller.signal, nextDescriptor);
        break;
      case 'spreadsheet':
        void loadSpreadsheetContent(requestId, controller.signal);
        break;
      case 'archive':
        void loadZipContent(requestId, controller.signal);
        break;
      case 'image':
      case 'pdf':
        void loadBinaryPreview(requestId, controller.signal);
        break;
      default:
        break;
    }

    return () => controller.abort();
  // Loader 使用本次 render 的 file/session 快照；requestIdRef 额外丢弃不遵守 abort 的迟到响应。
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [file?.modified, file?.name, file?.path, file?.size, file?.type, sessionId, previewUrlBuilder]);

  const markdownDirty = descriptor?.kind === 'markdown' && markdownDraft !== textContent;
  const previewDirty = markdownDirty || spreadsheetDirty;
  const previewSaving = savingMarkdown || savingSpreadsheet;

  useEffect(() => {
    onDirtyChangeRef.current?.(previewDirty);
  }, [previewDirty]);

  useEffect(() => {
    onSavingChangeRef.current?.(previewSaving);
  }, [previewSaving]);

  useEffect(() => {
    if (!previewDirty) return undefined;
    const handleBeforeUnload = (event: BeforeUnloadEvent) => {
      event.preventDefault();
      event.returnValue = '';
    };
    window.addEventListener('beforeunload', handleBeforeUnload);
    return () => window.removeEventListener('beforeunload', handleBeforeUnload);
  }, [previewDirty]);

  useEffect(() => {
    return () => {
      if (binaryPreviewUrl) URL.revokeObjectURL(binaryPreviewUrl);
    };
  }, [binaryPreviewUrl]);

  useEffect(() => {
    return () => {
      if (convertedPdfUrl) URL.revokeObjectURL(convertedPdfUrl);
    };
  }, [convertedPdfUrl]);

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

  const fetchPreviewResponse = async (url: string, signal: AbortSignal) => {
    const response = await fetch(url, {
      headers: { ...apiService.getAuthHeaders() },
      signal,
    });
    if (!response.ok) throw new Error(`Failed to load file: ${response.status}`);
    return response;
  };

  const loadTextContent = async (requestId: number, signal: AbortSignal) => {
    setLoading(true);
    setError('');
    try {
      const cacheKey = getPreviewCacheKey();
      const cached = cacheKey ? readPreviewCache(cacheKey) : null;
      if (cached?.kind === 'text') {
        if (requestId === requestIdRef.current) {
          setTextContent(cached.text);
          setMarkdownDraft(cached.text);
        }
        return;
      }

      const response = await fetchPreviewResponse(getPreviewApiUrl(), signal);
      const text = await response.text();
      if (requestId !== requestIdRef.current) return;
      if (file && resolvePreviewDescriptor(file).kind === 'html') {
        setHtmlFrameLoading(true);
      }
      setTextContent(text);
      setMarkdownDraft(text);
      if (cacheKey) writePreviewCache(cacheKey, { kind: 'text', text });
    } catch (err) {
      if (requestId !== requestIdRef.current || isAbortError(err)) return;
      console.error('Failed to load text content:', err);
      setError('加载文件内容失败');
    } finally {
      if (requestId === requestIdRef.current) setLoading(false);
    }
  };

  const loadDocxFallback = async (requestId: number, signal: AbortSignal) => {
    const cacheKey = getPreviewCacheKey('::mammoth');
    const cached = cacheKey ? readPreviewCache(cacheKey) : null;
    if (cached?.kind === 'docx') {
      if (requestId === requestIdRef.current) setDocxHtml(cached.html);
      return;
    }

    const response = await fetchPreviewResponse(getPreviewApiUrl(), signal);
    const arrayBuffer = await response.arrayBuffer();
    const result = await mammoth.convertToHtml({ arrayBuffer });
    if (requestId !== requestIdRef.current) return;
    const sanitizedHtml = DOMPurify.sanitize(result.value, {
      USE_PROFILES: { html: true },
      FORBID_TAGS: ['script', 'style', 'iframe', 'object', 'embed'],
      FORBID_ATTR: ['onerror', 'onload', 'onclick'],
    });
    setDocxHtml(sanitizedHtml);
    if (cacheKey) writePreviewCache(cacheKey, { kind: 'docx', html: sanitizedHtml });
    if (result.messages.length > 0) console.warn('DOCX conversion warnings:', result.messages);
  };

  const loadOfficePreview = async (
    requestId: number,
    signal: AbortSignal,
    officeDescriptor: PreviewDescriptor,
  ) => {
    setLoading(true);
    setError('');
    try {
      const pdfCacheKey = getPreviewCacheKey('::render=pdf');
      const cached = pdfCacheKey ? readPreviewCache(pdfCacheKey) : null;
      let pdfBlob: Blob;
      if (cached?.kind === 'binary') {
        pdfBlob = cached.blob;
      } else {
        const response = await fetchPreviewResponse(withRenderFormat(getPreviewApiUrl(), 'pdf'), signal);
        pdfBlob = await response.blob();
        const contentType = response.headers?.get?.('content-type') || pdfBlob.type;
        if (contentType && !contentType.toLowerCase().includes('pdf')) {
          throw new Error(`Unexpected converted content type: ${contentType}`);
        }
        if (pdfCacheKey) writePreviewCache(pdfCacheKey, { kind: 'binary', blob: pdfBlob });
      }
      if (requestId !== requestIdRef.current) return;
      setConvertedPdfUrl(URL.createObjectURL(pdfBlob));
      setLoading(false);
      return;
    } catch (conversionError) {
      if (requestId !== requestIdRef.current || isAbortError(conversionError)) return;
      console.warn('Server-side office preview is unavailable:', conversionError);
    }

    if (officeDescriptor.kind === 'document' && officeDescriptor.type === 'docx') {
      try {
        await loadDocxFallback(requestId, signal);
        if (requestId === requestIdRef.current) {
          setPreviewNotice('分页预览暂不可用，当前显示安全转换后的简化版式。');
        }
      } catch (fallbackError) {
        if (requestId !== requestIdRef.current || isAbortError(fallbackError)) return;
        console.error('Failed to load DOCX fallback:', fallbackError);
        setPreviewNotice('在线转换暂不可用，请下载原文件查看。');
      }
    } else if (requestId === requestIdRef.current) {
      setPreviewNotice(
        officeDescriptor.type === 'slides'
          ? '当前 .slides 文件没有可用的 PDF 转换器，且其私有结构无法在浏览器中可靠解码。'
          : '服务端暂时无法生成只读 PDF 预览，请下载原文件查看。',
      );
    }

    if (requestId === requestIdRef.current) setLoading(false);
  };

  const loadSpreadsheetContent = async (requestId: number, signal: AbortSignal) => {
    setLoading(true);
    setError('');
    try {
      const response = await fetchPreviewResponse(getPreviewApiUrl(), signal);
      const arrayBuffer = await response.arrayBuffer();
      if (requestId !== requestIdRef.current) return;
      setSpreadsheetSource(arrayBuffer);
    } catch (err) {
      if (requestId !== requestIdRef.current || isAbortError(err)) return;
      console.error('Failed to load spreadsheet content:', err);
      setError('加载电子表格失败');
    } finally {
      if (requestId === requestIdRef.current) setLoading(false);
    }
  };

  const loadZipContent = async (requestId: number, signal: AbortSignal) => {
    setLoading(true);
    setError('');
    try {
      if (file && file.size > MAX_ZIP_PREVIEW_BYTES) {
        throw new Error('ZIP 文件超过 10 MB，请下载后解压查看');
      }
      const cacheKey = getPreviewCacheKey();
      const cached = cacheKey ? readPreviewCache(cacheKey) : null;
      if (cached?.kind === 'zip') {
        if (requestId === requestIdRef.current) setZipEntries(cached.entries);
        return;
      }
      const response = await fetchPreviewResponse(getPreviewApiUrl(), signal);
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
      if (requestId !== requestIdRef.current || isAbortError(err)) return;
      console.error('Failed to load ZIP directory:', err);
      setError(err instanceof Error ? err.message : '加载 ZIP 目录失败');
    } finally {
      if (requestId === requestIdRef.current) setLoading(false);
    }
  };

  const loadBinaryPreview = async (requestId: number, signal: AbortSignal) => {
    setLoading(true);
    setError('');
    try {
      const cacheKey = getPreviewCacheKey();
      const cached = cacheKey ? readPreviewCache(cacheKey) : null;
      let blob: Blob;
      if (cached?.kind === 'binary') {
        blob = cached.blob;
      } else {
        const response = await fetchPreviewResponse(getPreviewApiUrl(), signal);
        blob = await response.blob();
        if (cacheKey) writePreviewCache(cacheKey, { kind: 'binary', blob });
      }
      if (requestId !== requestIdRef.current) return;
      setBinaryPreviewUrl(URL.createObjectURL(blob));
    } catch (err) {
      if (requestId !== requestIdRef.current || isAbortError(err)) return;
      console.error('Failed to load binary content:', err);
      setError('加载文件预览失败');
    } finally {
      if (requestId === requestIdRef.current) setLoading(false);
    }
  };

  const handleDownload = async () => {
    if (!file) return;
    try {
      if (onDownloadFile) await onDownloadFile(file);
      else await apiService.downloadFile(sessionId, file.path);
    } catch (err) {
      console.error('Failed to download file:', err);
      setError(formatDownloadError(err));
    }
  };

  const handleOpenHtml = () => {
    if (!file || !textContent) return;
    try {
      openHtmlBlobPreview(textContent, file.name);
    } catch (err) {
      console.error('Failed to open HTML in a new tab:', err);
      setError('无法在新标签页中打开 HTML');
    }
  };

  const handleSaveMarkdown = useCallback((): Promise<boolean> => {
    if (markdownSavePromiseRef.current) return markdownSavePromiseRef.current;
    if (
      !file
      || descriptor?.kind !== 'markdown'
      || effectiveReadOnly
    ) return Promise.resolve(true);

    const savePromise = (async () => {
      setSavingMarkdown(true);
      setMarkdownSaveError('');
      try {
        while (true) {
          const currentMarkdown = markdownEditorRef.current?.getMarkdown() ?? markdownDraftRef.current;
          if (currentMarkdown === textContentRef.current) {
            setMarkdownSaveMessage('已自动保存');
            setMarkdownSaveError('');
            return true;
          }

          const currentVersion = fileVersionRef.current;
          if (!currentVersion) return false;
          const revisionAtStart = markdownRevisionRef.current;
          setMarkdownSaveMessage('正在保存…');
          const updated = await apiService.updateSessionMarkdown(sessionId, currentVersion, currentMarkdown);
          const updatedWithSession = { ...updated, session_id: file.session_id || sessionId };
          selfSavedModifiedRef.current = updated.modified;
          fileVersionRef.current = updatedWithSession;
          textContentRef.current = currentMarkdown;
          setFileVersion(updatedWithSession);
          setTextContent(currentMarkdown);
          onFileUpdated?.(updatedWithSession);

          if (revisionAtStart === markdownRevisionRef.current) {
            markdownDraftRef.current = currentMarkdown;
            setMarkdownDraft(currentMarkdown);
            setMarkdownSaveMessage('已自动保存');
            return true;
          }
          // 用户在保存期间继续输入：立即用刚返回的新版本令牌串行保存最新草稿。
        }
      } catch (err) {
        console.error('Failed to save Markdown file:', err);
        setMarkdownSaveError(err instanceof Error ? err.message : 'Markdown 保存失败');
        setMarkdownSaveMessage('');
        onSaveFailureRef.current?.();
        return false;
      } finally {
        setSavingMarkdown(false);
      }
    })();
    markdownSavePromiseRef.current = savePromise;
    void savePromise.finally(() => {
      if (markdownSavePromiseRef.current === savePromise) markdownSavePromiseRef.current = null;
    });
    return savePromise;
  }, [
    descriptor?.kind,
    file,
    onFileUpdated,
    effectiveReadOnly,
    sessionId,
  ]);

  useEffect(() => {
    if (
      descriptor?.kind !== 'markdown'
      || effectiveReadOnly
      || !markdownDirty
      || savingMarkdown
      || markdownSaveError
    ) return undefined;
    const timer = window.setTimeout(() => {
      void handleSaveMarkdown();
    }, MARKDOWN_AUTOSAVE_DELAY_MS);
    return () => window.clearTimeout(timer);
  }, [
    descriptor?.kind,
    handleSaveMarkdown,
    markdownDirty,
    markdownRevision,
    markdownSaveError,
    effectiveReadOnly,
    savingMarkdown,
  ]);

  const markSpreadsheetDirty = useCallback(() => {
    spreadsheetRevisionRef.current += 1;
    spreadsheetDirtyRef.current = true;
    setSpreadsheetRevision(spreadsheetRevisionRef.current);
    setSpreadsheetDirty(true);
    setSpreadsheetSaveError('');
    setSpreadsheetSaveMessage('等待自动保存');
  }, []);

  const handleSaveSpreadsheet = useCallback((): Promise<boolean> => {
    if (spreadsheetSavePromiseRef.current) return spreadsheetSavePromiseRef.current;
    if (
      !file
      || descriptor?.kind !== 'spreadsheet'
      || spreadsheetReadOnly
    ) return Promise.resolve(true);

    const savePromise = (async () => {
      setSavingSpreadsheet(true);
      setSpreadsheetSaveError('');
      try {
        while (spreadsheetDirtyRef.current) {
          const currentVersion = fileVersionRef.current;
          if (!currentVersion) return false;
          const revisionAtStart = spreadsheetRevisionRef.current;
          setSpreadsheetSaveMessage('正在保存…');
          const content = spreadsheetEditorRef.current?.exportFile();
          if (!content) throw new Error('电子表格编辑器尚未准备好');
          const updated = await apiService.updateSessionSpreadsheet(sessionId, currentVersion, content);
          const updatedWithSession = { ...updated, session_id: file.session_id || sessionId };
          selfSavedModifiedRef.current = updated.modified;
          fileVersionRef.current = updatedWithSession;
          setFileVersion(updatedWithSession);
          onFileUpdated?.(updatedWithSession);
          if (revisionAtStart === spreadsheetRevisionRef.current) {
            spreadsheetDirtyRef.current = false;
            setSpreadsheetDirty(false);
            setSpreadsheetSaveMessage('已自动保存');
            return true;
          }
        }
        setSpreadsheetSaveMessage('已自动保存');
        return true;
      } catch (err) {
        console.error('Failed to save spreadsheet file:', err);
        setSpreadsheetSaveError(err instanceof Error ? err.message : '电子表格保存失败');
        setSpreadsheetSaveMessage('');
        onSaveFailureRef.current?.();
        return false;
      } finally {
        setSavingSpreadsheet(false);
      }
    })();
    spreadsheetSavePromiseRef.current = savePromise;
    void savePromise.finally(() => {
      if (spreadsheetSavePromiseRef.current === savePromise) spreadsheetSavePromiseRef.current = null;
    });
    return savePromise;
  }, [descriptor?.kind, file, onFileUpdated, sessionId, spreadsheetReadOnly]);

  useEffect(() => {
    if (
      descriptor?.kind !== 'spreadsheet'
      || spreadsheetReadOnly
      || !spreadsheetDirty
      || savingSpreadsheet
      || spreadsheetSaveError
    ) return undefined;
    const timer = window.setTimeout(() => {
      void handleSaveSpreadsheet();
    }, 1200);
    return () => window.clearTimeout(timer);
  }, [
    descriptor?.kind,
    handleSaveSpreadsheet,
    savingSpreadsheet,
    spreadsheetDirty,
    spreadsheetRevision,
    spreadsheetSaveError,
    spreadsheetReadOnly,
  ]);

  useEffect(() => {
    if (saveRequestNonce === undefined || handledSaveRequestNonceRef.current === saveRequestNonce) return;
    handledSaveRequestNonceRef.current = saveRequestNonce;
    if (effectiveReadOnly) return;
    if (descriptor?.kind === 'markdown' && markdownDirty && !savingMarkdown) {
      void handleSaveMarkdown();
    } else if (descriptor?.kind === 'spreadsheet' && spreadsheetDirty && !savingSpreadsheet) {
      void handleSaveSpreadsheet();
    }
  }, [
    descriptor?.kind,
    handleSaveMarkdown,
    handleSaveSpreadsheet,
    markdownDirty,
    effectiveReadOnly,
    saveRequestNonce,
    savingMarkdown,
    savingSpreadsheet,
    spreadsheetDirty,
  ]);

  useImperativeHandle(ref, () => ({
    ownerSessionId: sessionId,
    ownerEpoch,
    path: file?.path || '',
    isDirty: (expectedOwner) => (
      expectedOwner.ownerSessionId === sessionId
      && expectedOwner.ownerEpoch === ownerEpoch
      && !effectiveReadOnly
      && (
        (descriptor?.kind === 'markdown' && markdownDraftRef.current !== textContentRef.current)
        || (descriptor?.kind === 'spreadsheet' && spreadsheetDirtyRef.current)
      )
    ),
    saveDirty: async (expectedOwner) => {
      const path = file?.path || '';
      const expectedMatches = expectedOwner.ownerSessionId === sessionId
        && expectedOwner.ownerEpoch === ownerEpoch;
      if (!expectedMatches) {
        return { ...expectedOwner, path, ok: false, stale: true };
      }
      if (effectiveReadOnly) {
        return { ...expectedOwner, path, ok: true, stale: false };
      }

      let ok = true;
      const currentMarkdownDirty = descriptor?.kind === 'markdown'
        && markdownDraftRef.current !== textContentRef.current;
      if (currentMarkdownDirty) {
        ok = await handleSaveMarkdown();
      } else if (descriptor?.kind === 'spreadsheet' && spreadsheetDirtyRef.current) {
        ok = await handleSaveSpreadsheet();
      }

      const currentOwner = ownerIdentityRef.current;
      const stale = currentOwner.ownerSessionId !== expectedOwner.ownerSessionId
        || currentOwner.ownerEpoch !== expectedOwner.ownerEpoch;
      return { ...expectedOwner, path, ok: ok && !stale, stale };
    },
  }), [
    descriptor?.kind,
    effectiveReadOnly,
    file?.path,
    handleSaveMarkdown,
    handleSaveSpreadsheet,
    ownerEpoch,
    sessionId,
  ]);

  const scrollToMarkdownHeading = (text: string) => {
    const candidates = contentViewportRef.current?.querySelectorAll<HTMLElement>('h1, h2, h3');
    const target = Array.from(candidates || []).find(
      (heading) => heading.textContent?.replace(/\s+/g, ' ').trim() === text,
    );
    target?.scrollIntoView({ block: 'start', behavior: 'smooth' });
  };

  if (!file || !descriptor) return null;

  const filteredZipEntries = zipEntries.filter((entry) =>
    entry.path.toLocaleLowerCase().includes(zipQuery.trim().toLocaleLowerCase()),
  );

  const renderOfficeFallback = () => (
    <div className="flex h-full flex-col items-center justify-center px-6 py-12">
      <div className="mb-5 rounded-full bg-[#FF9500]/10 p-5">
        <Presentation className="h-14 w-14 text-[#D97706]" />
      </div>
      <div className="mb-5 w-full max-w-md rounded-2xl border border-black/[0.06] bg-white p-6 shadow-sm">
        <h4 className="mb-2 text-[16px] font-semibold text-claude-text">
          {descriptor.kind === 'presentation' ? '演示文稿' : '文档'}
        </h4>
        <p className="mb-4 text-[13px] leading-6 text-claude-secondary">
          {previewNotice || '当前环境无法生成在线预览，请下载原文件查看。'}
        </p>
        <dl className="space-y-2 text-[13px]">
          <div className="flex justify-between gap-4 border-b border-claude-border py-2">
            <dt className="text-claude-muted">文件名</dt>
            <dd className="truncate font-medium text-claude-text">{file.name}</dd>
          </div>
          <div className="flex justify-between gap-4 border-b border-claude-border py-2">
            <dt className="text-claude-muted">类型</dt>
            <dd className="font-medium text-claude-text">{getFileExtLabel(file)}</dd>
          </div>
          <div className="flex justify-between gap-4 py-2">
            <dt className="text-claude-muted">大小</dt>
            <dd className="font-medium text-claude-text">{formatFileSize(file.size)}</dd>
          </div>
        </dl>
      </div>
      <button type="button" onClick={handleDownload} className="inline-flex items-center gap-2 rounded-xl bg-black px-6 py-3 font-medium text-white hover:bg-black/80">
        <Download className="h-5 w-5" />
        下载原文件
      </button>
    </div>
  );

  const renderPreviewBody = () => {
    switch (descriptor.kind) {
      case 'image':
        return (
          <div className="flex h-full min-h-[320px] flex-col">
            <div className="mb-3 flex justify-center gap-1">
              <button type="button" onClick={() => setImageScale((value) => Math.max(0.25, value - 0.25))} className="rounded-md p-2 text-claude-secondary hover:bg-claude-hover" title="缩小"><ZoomOut size={16} /></button>
              <span className="min-w-[54px] self-center text-center text-xs text-claude-muted">{Math.round(imageScale * 100)}%</span>
              <button type="button" onClick={() => setImageScale((value) => Math.min(4, value + 0.25))} className="rounded-md p-2 text-claude-secondary hover:bg-claude-hover" title="放大"><ZoomIn size={16} /></button>
              <button type="button" onClick={() => setImageRotation((value) => (value + 90) % 360)} className="rounded-md p-2 text-claude-secondary hover:bg-claude-hover" title="顺时针旋转"><RotateCcw className="-scale-x-100" size={16} /></button>
              <button type="button" onClick={() => { setImageScale(1); setImageRotation(0); }} className="rounded-md px-2 text-xs text-claude-secondary hover:bg-claude-hover" title="恢复原始视图">适应</button>
            </div>
            <div className="flex flex-1 items-center justify-center overflow-auto rounded-2xl border border-black/[0.05] bg-[linear-gradient(45deg,#f3f3f3_25%,transparent_25%),linear-gradient(-45deg,#f3f3f3_25%,transparent_25%),linear-gradient(45deg,transparent_75%,#f3f3f3_75%),linear-gradient(-45deg,transparent_75%,#f3f3f3_75%)] bg-[length:20px_20px] bg-[position:0_0,0_10px,10px_-10px,-10px_0px] p-6">
              <img
                src={binaryPreviewUrl}
                alt={file.name}
                className="max-h-[70vh] max-w-full object-contain transition-transform duration-150"
                style={{ transform: `rotate(${imageRotation}deg) scale(${imageScale})` }}
                onError={() => setError('图片加载失败')}
              />
            </div>
          </div>
        );
      case 'pdf':
        return <PdfFrame src={binaryPreviewUrl} title={file.name} onError={() => setError('PDF 加载失败')} />;
      case 'markdown':
        if (!effectiveReadOnly) {
          return (
            <Suspense fallback={<div className="flex h-full items-center justify-center text-sm text-claude-muted">正在打开 Markdown 编辑器…</div>}>
              <VditorMarkdownEditor
                ref={markdownEditorRef}
                markdown={markdownDraft}
                filePath={file.path}
                buildSessionFileUrl={buildMarkdownSessionFileUrl}
                toolbarOpen={markdownToolbarOpen}
                onChange={(nextMarkdown) => {
                  markdownRevisionRef.current += 1;
                  markdownDraftRef.current = nextMarkdown;
                  setMarkdownRevision(markdownRevisionRef.current);
                  setMarkdownDraft(nextMarkdown);
                  setMarkdownSaveMessage(nextMarkdown === textContent ? '' : '等待自动保存');
                  setMarkdownSaveError('');
                }}
              />
            </Suspense>
          );
        }
        return (
          <MarkdownReportPreview
            content={markdownDraft}
            filePath={file.path}
            buildSessionFileUrl={buildMarkdownSessionFileUrl}
          />
        );
      case 'html':
        return viewMode === 'rendered' ? (
          <HtmlFrame
            html={textContent}
            title={file.name}
            loading={htmlFrameLoading}
            onLoadingChange={setHtmlFrameLoading}
            onError={() => setError('HTML 加载失败')}
          />
        ) : renderSource(textContent, 'html', wrapLongLines);
      case 'code':
        return renderSource(textContent, descriptor.language || descriptor.type, wrapLongLines);
      case 'text':
        return (
          <div className="mx-auto max-w-4xl rounded-xl border border-black/[0.05] bg-white p-6 shadow-sm">
            <pre className={`font-mono text-[13px] leading-6 text-claude-text ${wrapLongLines ? 'whitespace-pre-wrap break-words' : 'overflow-x-auto whitespace-pre'}`}>{textContent}</pre>
          </div>
        );
      case 'document':
        if (convertedPdfUrl) {
          return <PdfFrame src={convertedPdfUrl} title={`${file.name} PDF 预览`} onError={() => setError('PDF 加载失败')} />;
        }
        if (docxHtml) {
          return (
            <div className="mx-auto max-w-[820px]">
              {previewNotice && <div className="mb-3 rounded-lg border border-amber-200 bg-amber-50 px-4 py-2 text-xs text-amber-800">{previewNotice}</div>}
              <article className="file-preview-report prose" dangerouslySetInnerHTML={{ __html: docxHtml }} />
            </div>
          );
        }
        return renderOfficeFallback();
      case 'spreadsheet':
        if (!spreadsheetSource) {
          return <div className="flex h-full items-center justify-center text-sm text-claude-muted">电子表格内容为空</div>;
        }
        return (
          <Suspense fallback={<div className="flex h-full items-center justify-center text-sm text-claude-muted">正在启动电子表格编辑器…</div>}>
            <SpreadsheetEditor
              ref={spreadsheetEditorRef}
              source={spreadsheetSource}
              fileName={file.name}
              fileType={descriptor.type as 'csv' | 'xls' | 'xlsx' | 'et'}
              readOnly={spreadsheetReadOnly}
              onMutation={spreadsheetReadOnly ? undefined : markSpreadsheetDirty}
              onError={setError}
            />
          </Suspense>
        );
      case 'archive':
        return (
          <div className="mx-auto w-full max-w-4xl overflow-hidden rounded-2xl border border-black/[0.06] bg-white shadow-sm">
            <div className="flex flex-wrap items-center justify-between gap-3 border-b border-claude-border px-4 py-3">
              <div className="flex items-center gap-2 text-sm font-semibold text-claude-text">
                <Archive size={17} className="text-claude-accent" />
                压缩包目录
                <span className="text-xs font-normal text-claude-muted">{zipEntries.length} 项（只读）</span>
              </div>
              <label className="flex h-8 min-w-[180px] items-center gap-2 rounded-lg bg-claude-surface px-2 text-claude-muted">
                <Search size={14} aria-hidden />
                <span className="sr-only">搜索压缩包目录</span>
                <input value={zipQuery} onChange={(event) => setZipQuery(event.target.value)} className="min-w-0 flex-1 bg-transparent text-xs text-claude-text outline-none" placeholder="搜索目录" />
              </label>
            </div>
            {filteredZipEntries.length === 0 ? (
              <div className="py-16 text-center text-sm text-claude-muted">{zipEntries.length === 0 ? '压缩包为空' : '没有匹配项'}</div>
            ) : (
              <ul className="max-h-[65vh] divide-y divide-claude-border/60 overflow-y-auto">
                {filteredZipEntries.map((entry) => (
                  <li key={`${entry.directory ? 'd' : 'f'}:${entry.path}`} className="flex items-center gap-3 px-4 py-2.5">
                    {entry.directory ? <Folder size={15} className="shrink-0 text-claude-accent" /> : <File size={15} className="shrink-0 text-claude-muted" />}
                    <span className="min-w-0 break-all font-mono text-xs text-claude-text">{entry.path}</span>
                  </li>
                ))}
              </ul>
            )}
          </div>
        );
      case 'presentation':
        return convertedPdfUrl
          ? <SlideDeckPreview src={convertedPdfUrl} title={file.name} />
          : renderOfficeFallback();
      default:
        return (
          <div className="py-24 text-center">
            <AlertCircle className="mx-auto mb-3 h-12 w-12 text-claude-border" />
            <p className="mb-4 text-claude-muted">此文件类型不支持预览</p>
            <button type="button" onClick={handleDownload} className="inline-flex items-center gap-2 rounded-xl bg-black px-6 py-3 font-medium text-white hover:bg-black/80"><Download className="h-4 w-4" />下载文件</button>
          </div>
        );
    }
  };

  const cardClassName = inline
    ? 'h-full flex flex-col bg-white min-w-0'
    : 'relative w-full max-w-[1180px] h-full bg-white rounded-[28px] shadow-2xl flex flex-col overflow-hidden animate-zoom-in';
  const headerClassName = inline
    ? 'h-11 border-b border-black/[0.06] flex items-center justify-between gap-2 px-3 shrink-0 bg-white'
    : 'h-16 border-b border-black/[0.06] flex items-center justify-between gap-3 px-6 shrink-0 bg-white/90 backdrop-blur-xl';
  const isRenderedMarkdownWorkspace = descriptor.kind === 'markdown';
  const shouldFillViewport = ['markdown', 'html', 'pdf', 'document', 'spreadsheet', 'presentation'].includes(descriptor.kind);
  const contentClassName = inline
    ? shouldFillViewport
      ? `flex-1 min-h-0 min-w-0 overflow-hidden ${isRenderedMarkdownWorkspace ? 'bg-[#f1f1ef]' : 'bg-white'}`
      : 'flex-1 min-h-0 min-w-0 overflow-auto p-4 bg-claude-bg'
    : isRenderedMarkdownWorkspace
      ? 'flex-1 min-h-0 min-w-0 overflow-hidden bg-[#f1f1ef]'
      : descriptor.kind === 'spreadsheet'
        ? 'flex-1 min-h-0 min-w-0 overflow-hidden bg-white'
        : 'flex-1 min-h-0 min-w-0 overflow-auto p-6 sm:p-8 bg-claude-bg';
  const closeButtonClassName = inline
    ? 'rounded-md p-2 text-claude-muted transition-colors hover:bg-claude-hover'
    : 'rounded-full bg-black p-2.5 text-white shadow-lg transition-[opacity,transform] hover:opacity-80 active:scale-90';
  const FileIcon = getPreviewIcon(descriptor);
  const supportsViewToggle = descriptor.kind === 'html';
  const supportsMarkdownEdit = descriptor.kind === 'markdown' && !effectiveReadOnly;
  const supportsWrapToggle = viewMode === 'source' || descriptor.kind === 'code' || descriptor.kind === 'text';

  const previewCard = (
    <div className={cardClassName} onClick={inline ? undefined : (event) => event.stopPropagation()} data-testid={inline ? 'file-preview-inline' : undefined}>
      <div className={headerClassName}>
        <div className="flex min-w-0 items-center gap-2">
          {descriptor.kind === 'markdown' && markdownHeadings.length > 1 && (
            <button
              type="button"
              aria-label={markdownTocOpen ? '收起目录' : '展开目录'}
              aria-expanded={markdownTocOpen}
              onClick={() => setMarkdownTocOpen((open) => !open)}
              className={`inline-flex h-7 w-7 shrink-0 items-center justify-center rounded-md transition-colors ${markdownTocOpen ? 'bg-claude-file/10 text-claude-file' : 'text-claude-muted hover:bg-claude-hover hover:text-claude-text'}`}
              title={markdownTocOpen ? '收起目录' : '展开目录'}
            >
              {markdownTocOpen ? <PanelLeftClose size={16} aria-hidden="true" /> : <PanelLeftOpen size={16} aria-hidden="true" />}
            </button>
          )}
          <div className="flex h-7 w-7 shrink-0 items-center justify-center rounded-md bg-claude-surface text-claude-accent"><FileIcon size={16} aria-hidden="true" /></div>
          <h2 className="min-w-0 truncate text-[13px] font-semibold tracking-tight text-claude-text">{file.name}</h2>
          <span className="hidden shrink-0 text-[10px] uppercase tracking-wider text-claude-muted sm:inline">{getFileExtLabel(file)} · {formatFileSize(fileVersion?.size ?? file.size)}</span>
          {descriptor.kind === 'spreadsheet' && fileVersion?.modified && (
            <span className="hidden shrink-0 text-[11px] text-claude-muted lg:inline" data-testid="spreadsheet-modified-time">
              最近修改：{formatModifiedTime(fileVersion.modified)}
            </span>
          )}
          {descriptor.kind === 'spreadsheet' && spreadsheetReadOnly && (
            <span className="shrink-0 rounded bg-claude-surface px-1.5 py-0.5 text-[10px] font-medium text-claude-muted" role="status">
              只读
            </span>
          )}
        </div>
        <div className="flex shrink-0 items-center gap-1">
          {markdownSaveError && <span role="alert" title={markdownSaveError} className="file-preview-save-state file-preview-save-state--error">保存失败</span>}
          {markdownSaveMessage && !markdownSaveError && (
            <span role="status" className={`file-preview-save-state ${markdownSaveMessage === '已自动保存' ? 'file-preview-save-state--saved' : ''}`}>
              {markdownSaveMessage}
            </span>
          )}
          {markdownDirty && !markdownSaveError && !markdownSaveMessage && <span className="file-preview-save-state">等待自动保存</span>}
          {descriptor.kind === 'spreadsheet' && spreadsheetSaveError && (
            <span role="alert" title={spreadsheetSaveError} className="max-w-[120px] truncate text-[11px] text-claude-error">保存失败</span>
          )}
          {descriptor.kind === 'spreadsheet' && spreadsheetSaveMessage && !spreadsheetSaveError && (
            <span role="status" className={`text-[11px] ${spreadsheetSaveMessage === '已自动保存' ? 'text-claude-success' : 'text-claude-muted'}`}>
              {spreadsheetSaveMessage}
            </span>
          )}
          {descriptor.kind === 'spreadsheet' && spreadsheetSaveError && !spreadsheetReadOnly && (
            <button
              type="button"
              onClick={() => void handleSaveSpreadsheet()}
              disabled={savingSpreadsheet}
              className="rounded-md p-1.5 text-claude-muted transition-colors hover:bg-claude-hover hover:text-claude-text disabled:cursor-wait disabled:opacity-50"
              title="重试保存电子表格"
              aria-label="重试保存电子表格"
            >
              <Save size={16} aria-hidden="true" />
            </button>
          )}
          {supportsViewToggle && (
            <div className="mr-1 flex rounded-lg bg-claude-surface p-0.5">
              <button type="button" aria-label="阅读视图" aria-pressed={viewMode === 'rendered'} onClick={() => setViewMode('rendered')} className={`rounded-md p-1.5 transition-colors ${viewMode === 'rendered' ? 'bg-white text-claude-text shadow-sm' : 'text-claude-muted'}`} title="阅读视图"><Eye size={15} aria-hidden="true" /></button>
              <button type="button" aria-label="源代码" aria-pressed={viewMode === 'source'} onClick={() => setViewMode('source')} className={`rounded-md p-1.5 transition-colors ${viewMode === 'source' ? 'bg-white text-claude-text shadow-sm' : 'text-claude-muted'}`} title="源代码"><Code size={15} aria-hidden="true" /></button>
            </div>
          )}
          {supportsMarkdownEdit && (
            <button
              type="button"
              aria-label={markdownToolbarOpen ? '收起 Markdown 格式工具' : '展开 Markdown 格式工具'}
              aria-expanded={markdownToolbarOpen}
              onClick={() => setMarkdownToolbarOpen((open) => !open)}
              className={`rounded-md p-1.5 transition-colors hover:bg-claude-hover hover:text-claude-text ${markdownToolbarOpen ? 'bg-claude-hover text-claude-text' : 'text-claude-muted'}`}
              title={markdownToolbarOpen ? '收起格式工具' : '展开格式工具'}
            >
              <MoreHorizontal size={17} aria-hidden="true" />
            </button>
          )}
          {supportsMarkdownEdit && markdownSaveError && (
            <button type="button" onClick={() => void handleSaveMarkdown()} disabled={savingMarkdown} className="rounded-md p-1.5 text-claude-muted transition-colors hover:bg-claude-hover hover:text-claude-text disabled:cursor-wait disabled:opacity-50" title="重试保存 Markdown" aria-label="重试保存 Markdown"><Save size={16} aria-hidden="true" /></button>
          )}
          {supportsWrapToggle && (
            <button type="button" aria-label={wrapLongLines ? '关闭自动换行' : '开启自动换行'} aria-pressed={wrapLongLines} onClick={() => setWrapLongLines((value) => !value)} className={`rounded-md p-1.5 transition-colors hover:bg-claude-hover ${wrapLongLines ? 'text-claude-accent' : 'text-claude-muted'}`} title={wrapLongLines ? '关闭自动换行' : '开启自动换行'}><WrapText size={16} aria-hidden="true" /></button>
          )}
          {descriptor.kind === 'html' && viewMode === 'rendered' && (
            <button type="button" onClick={handleOpenHtml} className="rounded-md p-1.5 text-claude-muted transition-colors hover:bg-claude-hover hover:text-claude-text" title="在新标签页中查看" aria-label="在新标签页中查看"><ExternalLink size={16} aria-hidden="true" /></button>
          )}
          <button type="button" onClick={handleDownload} className="rounded-md p-1.5 text-claude-muted transition-colors hover:bg-claude-hover hover:text-claude-text" title="下载文件" aria-label="下载文件"><Download size={16} aria-hidden="true" /></button>
          {!inline && <button type="button" ref={closeButtonRef} onClick={onClose} className={closeButtonClassName} title="关闭" aria-label="关闭"><X size={17} aria-hidden="true" /></button>}
        </div>
      </div>

      <div ref={contentViewportRef} className={`${contentClassName} relative`}>
        {descriptor.kind === 'markdown' && markdownTocOpen && markdownHeadings.length > 1 && (
          <aside className="absolute inset-y-0 left-0 z-30 flex w-[224px] flex-col border-r border-claude-border bg-white shadow-xl" aria-label="Markdown 目录">
            <div className="flex h-10 shrink-0 items-center justify-between border-b border-claude-border px-3">
              <span className="text-xs font-semibold text-claude-text">目录</span>
              <button type="button" onClick={() => setMarkdownTocOpen(false)} className="rounded p-1 text-claude-muted hover:bg-claude-hover" aria-label="收起目录"><X size={14} aria-hidden="true" /></button>
            </div>
            <nav className="min-h-0 flex-1 overflow-y-auto p-2">
              {markdownHeadings.map((heading) => (
                <button
                  key={heading.id}
                  type="button"
                  onClick={() => scrollToMarkdownHeading(heading.text)}
                  className="block w-full truncate rounded-md px-2 py-1.5 text-left text-xs text-claude-secondary hover:bg-claude-hover hover:text-claude-text"
                  style={{ paddingLeft: `${8 + (heading.depth - 1) * 10}px` }}
                  title={heading.text}
                >
                  {heading.text}
                </button>
              ))}
            </nav>
          </aside>
        )}
        {error ? (
          <div className="flex items-center gap-3 rounded-2xl border border-red-100 bg-red-50 p-4"><AlertCircle className="h-5 w-5 text-claude-error" /><span className="font-medium text-claude-error">{error}</span></div>
        ) : loading ? (
          <div className="flex items-center justify-center py-24" data-testid="file-preview-loading">
            <div className="flex space-x-2">{[0, 1, 2].map((index) => <div key={index} className="h-2 w-2 animate-dot-pulse rounded-full bg-claude-accent" style={{ animationDelay: `${index * 200}ms` }} />)}</div>
          </div>
        ) : renderPreviewBody()}
      </div>
    </div>
  );

  if (inline) return previewCard;

  return (
    <div className="fixed inset-0 z-[100] flex animate-fade-in items-center justify-center p-6 sm:p-12" onClick={onClose}>
      <div className="absolute inset-0 bg-black/40 backdrop-blur-md" />
      {previewCard}
    </div>
  );
});

interface PdfFrameProps {
  src: string;
  title: string;
  onError: () => void;
}

function PdfFrame({ src, title, onError }: PdfFrameProps) {
  const [ready, setReady] = useState(false);

  useEffect(() => {
    setReady(false);
  }, [src]);

  return (
    <div className="relative h-full min-h-0 w-full overflow-hidden bg-white">
      <iframe
        src={src}
        className={`h-full min-h-0 w-full border-0 bg-white transition-opacity duration-150 ${ready ? 'opacity-100' : 'opacity-0'}`}
        title={title}
        onLoad={() => setReady(true)}
        onError={() => {
          setReady(true);
          onError();
        }}
      />
      {!ready && (
        <div className="absolute inset-0 flex items-center justify-center bg-white" data-testid="pdf-iframe-loading">
          <div className="flex space-x-2">
            {[0, 1, 2].map((index) => (
              <div
                key={index}
                className="h-2 w-2 animate-dot-pulse rounded-full bg-claude-accent"
                style={{ animationDelay: `${index * 200}ms` }}
              />
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

interface HtmlFrameProps {
  html: string;
  title: string;
  loading: boolean;
  onLoadingChange: (loading: boolean) => void;
  onError: () => void;
}

export const HTML_INLINE_PREVIEW_TIMEOUT_MS = 10_000;

export function scheduleHtmlInlinePreviewTimeout(
  onTimeout: () => void,
  timeoutMs = HTML_INLINE_PREVIEW_TIMEOUT_MS,
): number {
  return window.setTimeout(onTimeout, timeoutMs);
}

function HtmlFrame({ html, title, loading, onLoadingChange, onError }: HtmlFrameProps) {
  const timeoutRef = useRef<number | null>(null);
  const onErrorRef = useRef(onError);
  onErrorRef.current = onError;

  const clearLoadTimeout = () => {
    if (timeoutRef.current === null) return;
    window.clearTimeout(timeoutRef.current);
    timeoutRef.current = null;
  };

  useEffect(() => {
    if (!html) {
      onLoadingChange(false);
      return undefined;
    }

    onLoadingChange(true);
    timeoutRef.current = scheduleHtmlInlinePreviewTimeout(() => {
      timeoutRef.current = null;
      onLoadingChange(false);
      onErrorRef.current();
    });

    return clearLoadTimeout;
  }, [html, onLoadingChange]);

  const finishLoading = () => {
    clearLoadTimeout();
    onLoadingChange(false);
  };

  const failLoading = () => {
    finishLoading();
    onErrorRef.current();
  };

  return (
    <div className="relative h-full min-h-0 w-full overflow-hidden bg-white">
      {loading && (
        <div className="absolute inset-0 z-10 flex items-center justify-center bg-white/90" data-testid="html-iframe-loading">
          <div className="flex space-x-2">{[0, 1, 2].map((index) => <div key={index} className="h-2 w-2 animate-dot-pulse rounded-full bg-claude-accent" style={{ animationDelay: `${index * 200}ms` }} />)}</div>
        </div>
      )}
      {html && (
        <iframe
          srcDoc={html}
          className="h-full w-full border-0"
          title={title}
          sandbox="allow-scripts"
          referrerPolicy="no-referrer"
          onLoad={finishLoading}
          onError={failLoading}
        />
      )}
    </div>
  );
}

function renderSource(content: string, language: string, wrapLongLines: boolean) {
  return (
    <div className="overflow-hidden rounded-xl bg-[#1C1C1E] shadow-xl ring-1 ring-white/10">
      <SyntaxHighlighter
        language={language}
        style={vscDarkPlus}
        showLineNumbers
        wrapLongLines={wrapLongLines}
        customStyle={{ margin: 0, borderRadius: 0, fontSize: '13px', lineHeight: 1.65, background: 'transparent' }}
      >
        {content}
      </SyntaxHighlighter>
    </div>
  );
}

function getPreviewIcon(descriptor: PreviewDescriptor) {
  switch (descriptor.kind) {
    case 'image': return FileImage;
    case 'spreadsheet': return FileSpreadsheet;
    case 'code':
    case 'html': return FileCode;
    case 'presentation': return Presentation;
    case 'archive': return Archive;
    case 'document':
    case 'pdf':
    case 'markdown':
    case 'text': return FileText;
    default: return File;
  }
}

function formatFileSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function formatModifiedTime(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat('zh-CN', {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  }).format(date);
}

/** 处理引号、引号内换行及双引号转义；只负责预览，不做类型推断。 */
export function parseCsvRows(source: string): string[][] {
  const rows: string[][] = [];
  let row: string[] = [];
  let value = '';
  let quoted = false;

  for (let index = 0; index < source.length; index += 1) {
    const character = source[index];
    if (character === '"') {
      if (quoted && source[index + 1] === '"') {
        value += '"';
        index += 1;
      } else {
        quoted = !quoted;
      }
    } else if (character === ',' && !quoted) {
      row.push(value);
      value = '';
    } else if ((character === '\n' || character === '\r') && !quoted) {
      if (character === '\r' && source[index + 1] === '\n') index += 1;
      row.push(value);
      if (row.some((cell) => cell.length > 0)) rows.push(row);
      row = [];
      value = '';
    } else {
      value += character;
    }
  }

  row.push(value);
  if (row.some((cell) => cell.length > 0)) rows.push(row);
  return rows;
}
