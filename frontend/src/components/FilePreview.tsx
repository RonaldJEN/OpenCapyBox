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
import { WorkspaceApiError, workspaceApi } from '../services/workspaceApi';
import { readFilePreviewCache, writeFilePreviewCache } from '../services/filePreviewCache';
import {
  discardSessionDraft,
  flushSessionDraft,
  getSessionDraft,
  queueSessionMarkdownDraft,
  queueSessionSpreadsheetDraft,
  resolveSessionDraftForRead,
  subscribeSessionSaves,
  getSessionSaveSnapshot,
  hasPendingSessionDraft,
  sessionDraftKey,
  type SessionSaveSnapshot,
  type SessionDraftRecord,
} from '../services/sessionDraftOutbox';
import {
  checkpointWorkspaceFile,
  flushWorkspaceDraft,
  getWorkspaceDraft,
  queueWorkspaceMarkdownDraft,
  queueWorkspaceSpreadsheetDraft,
  subscribeWorkspaceDraftLosses,
  takeWorkspaceDraftLossNotice,
  type WorkspaceDraftLossNotice,
} from '../services/workspaceDraftOutbox';
import { requestOpenWorkspace } from '../services/workspaceEvents';
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
import { DEFAULT_FEEDBACK_AUTO_DISMISS_MS } from './FeedbackMessage';
import { WorkspaceDestinationPicker } from './workspace/WorkspaceDestinationPicker';

interface FilePreviewProps {
  file: FileInfo | null;
  sessionId: string;
  ownerEpoch?: number;
  onClose: () => void;
  readOnly?: boolean;
  previewUrlBuilder?: (file: FileInfo) => string;
  onDownloadFile?: (file: FileInfo) => Promise<void>;
  onSaveMarkdownFile?: (file: FileInfo, content: string) => Promise<FileInfo>;
  onSaveSpreadsheetFile?: (file: FileInfo, content: ArrayBuffer) => Promise<FileInfo>;
  canSaveToWorkspace?: boolean;
  onOpenWorkspace?: () => void;
  onFileUpdated?: (file: FileInfo) => void;
  onDirtyChange?: (dirty: boolean) => void;
  onSavingChange?: (saving: boolean) => void;
  onSaveFailure?: () => void;
  saveRequestNonce?: number;
  refreshInPlace?: boolean;
  externalConflict?: boolean;
  reloadNonce?: number;
  onFileVersionLoaded?: (file: FileInfo) => void;
  inline?: boolean;
  contextNotice?: string;
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

export interface FilePreviewSaveOptions {
  requireRemote?: boolean;
}

export interface FilePreviewHandle extends SessionFileOwnerIdentity {
  path: string;
  isDirty: (expectedOwner: SessionFileOwnerIdentity) => boolean;
  saveDirty: (
    expectedOwner: SessionFileOwnerIdentity,
    options?: FilePreviewSaveOptions,
  ) => Promise<FilePreviewSaveResult>;
  downloadDraft: (expectedOwner: SessionFileOwnerIdentity) => boolean;
}

type PreviewViewMode = 'rendered' | 'source';
type PreviewLoadState = 'idle' | 'initial-loading' | 'ready' | 'refreshing' | 'error';

export const MARKDOWN_AUTOSAVE_DELAY_MS = 300;
export const SPREADSHEET_AUTOSAVE_DELAY_MS = 300;
export const OFFICE_PREVIEW_SLOW_HINT_MS = 2500;

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

interface PresentationPreviewSource {
  blob: Blob;
  key: string;
  requestId: number;
}

const MAX_ZIP_PREVIEW_BYTES = 10 * 1024 * 1024;
const MAX_ZIP_ENTRIES = 2000;

function isAbortError(error: unknown): boolean {
  return error instanceof DOMException && error.name === 'AbortError';
}

function previewOwnerKey(sessionId: string, ownerEpoch: number, file: FileInfo): string {
  const mode = file.content_mode || 'current';
  if (mode === 'captured') {
    return [
      sessionId,
      ownerEpoch,
      mode,
      file.source || 'session',
      file.entry_id || '',
      file.version_id || '',
      file.snapshot_path || '',
      file.assistant_ref_id || '',
      file.path,
    ].join('::');
  }
  return [
    sessionId,
    ownerEpoch,
    mode,
    file.source || 'session',
    file.source === 'workspace' && file.entry_id ? file.entry_id : file.path,
  ].join('::');
}

function previewContentKey(file: FileInfo): string {
  if (file.source === 'workspace' && file.version_id) return `workspace-version:${file.version_id}`;
  return [
    file.version_id || '',
    file.snapshot_path || '',
    file.assistant_ref_id || '',
    String(file.revision ?? ''),
    file.modified,
    file.size,
  ].join('::');
}

async function fetchMergedWorkspaceVersion(file: FileInfo): Promise<Response> {
  if (!file.entry_id || !file.version_id) throw new Error('合并回执缺少工作区版本');
  const response = await fetch(workspaceApi.versionContentUrl(file.version_id, true), {
    headers: apiService.getAuthHeaders(),
  });
  if (!response.ok) throw new Error(`读取合并版本失败（HTTP ${response.status}）`);
  return response;
}

function conflictCurrentFile(error: unknown): Partial<FileInfo> | null {
  const detail = (error as { detail?: unknown })?.detail;
  if (!detail || typeof detail !== 'object') return null;
  const current = (detail as { current?: unknown }).current;
  return current && typeof current === 'object' && typeof (current as { path?: unknown }).path === 'string'
    ? current as Partial<FileInfo>
    : null;
}

function isExpectedWorkspaceSaveWait(error: unknown): boolean {
  return error instanceof WorkspaceApiError && (
    error.detail.code === 'MUTATION_IN_PROGRESS'
    || error.detail.code === 'WORKSPACE_MUTATION_IN_PROGRESS'
  );
}

export const FilePreview = forwardRef<FilePreviewHandle, FilePreviewProps>(function FilePreview({
  file,
  sessionId,
  ownerEpoch = 0,
  onClose,
  readOnly = false,
  previewUrlBuilder,
  onDownloadFile,
  onSaveMarkdownFile,
  onSaveSpreadsheetFile,
  canSaveToWorkspace = false,
  onOpenWorkspace,
  onFileUpdated,
  onDirtyChange,
  onSavingChange,
  onSaveFailure,
  saveRequestNonce,
  refreshInPlace = false,
  externalConflict = false,
  reloadNonce = 0,
  onFileVersionLoaded,
  inline = false,
  contextNotice,
}: FilePreviewProps, ref) {
  const [previewLoadState, setPreviewLoadState] = useState<PreviewLoadState>('idle');
  const [officePreviewSlow, setOfficePreviewSlow] = useState(false);
  const [error, setError] = useState('');
  const [previewNotice, setPreviewNotice] = useState('');
  const [textContent, setTextContent] = useState('');
  const [markdownDraft, setMarkdownDraft] = useState('');
  const [fileVersion, setFileVersion] = useState<FileInfo | null>(file);
  const [savingMarkdown, setSavingMarkdown] = useState(false);
  const [markdownSaveError, setMarkdownSaveError] = useState('');
  const [markdownRevision, setMarkdownRevision] = useState(0);
  const [markdownToolbarOpen, setMarkdownToolbarOpen] = useState(false);
  const [markdownTocOpen, setMarkdownTocOpen] = useState(false);
  const [docxHtml, setDocxHtml] = useState('');
  const [spreadsheetSource, setSpreadsheetSource] = useState<ArrayBuffer | null>(null);
  const [spreadsheetDirty, setSpreadsheetDirty] = useState(false);
  const [spreadsheetRevision, setSpreadsheetRevision] = useState(0);
  const [savingSpreadsheet, setSavingSpreadsheet] = useState(false);
  const [spreadsheetSaveError, setSpreadsheetSaveError] = useState('');
  const [zipEntries, setZipEntries] = useState<ZipEntry[]>([]);
  const [zipQuery, setZipQuery] = useState('');
  const [binaryPreviewUrl, setBinaryPreviewUrl] = useState('');
  const [convertedPdfUrl, setConvertedPdfUrl] = useState('');
  const [presentationSource, setPresentationSource] = useState<PresentationPreviewSource | null>(null);
  const [presentationRequestId, setPresentationRequestId] = useState(0);
  const [htmlFrameLoading, setHtmlFrameLoading] = useState(false);
  const [viewMode, setViewMode] = useState<PreviewViewMode>('rendered');
  const [wrapLongLines, setWrapLongLines] = useState(false);
  const [imageScale, setImageScale] = useState(1);
  const [imageRotation, setImageRotation] = useState(0);
  const [workspacePickerOpen, setWorkspacePickerOpen] = useState(false);
  const [localReloadNonce, setLocalReloadNonce] = useState(0);
  const [workspaceImportPreparing, setWorkspaceImportPreparing] = useState(false);
  const [retainedSessionDraft, setRetainedSessionDraft] = useState<SessionDraftRecord | null>(null);
  const [workspaceNotice, setWorkspaceNotice] = useState<{
    entryId: string;
    path: string;
    message: string;
  } | null>(null);
  const [workspaceDraftLossNotice, setWorkspaceDraftLossNotice] = useState<WorkspaceDraftLossNotice | null>(null);
  const loading = previewLoadState === 'initial-loading' || previewLoadState === 'refreshing';
  const backgroundRefreshing = previewLoadState === 'refreshing';
  const hasCurrentPresentationSource = Boolean(
    presentationSource && presentationSource.requestId === presentationRequestId,
  );
  const requestIdRef = useRef(0);
  const closeButtonRef = useRef<HTMLButtonElement>(null);
  const contentViewportRef = useRef<HTMLDivElement>(null);
  const markdownEditorRef = useRef<VditorMarkdownEditorHandle>(null);
  const markdownRevisionRef = useRef(0);
  const spreadsheetEditorRef = useRef<SpreadsheetEditorHandle>(null);
  const spreadsheetRevisionRef = useRef(0);
  const draftOutboxGenerationRef = useRef(0);
  const acceptedSessionSaveRef = useRef<SessionSaveSnapshot | null>(null);
  const spreadsheetDraftKeyRef = useRef<string | null>(null);
  const spreadsheetDraftCapturePromiseRef = useRef<Promise<void> | null>(null);
  const selfSavedModifiedRef = useRef('');
  const loadedPreviewOwnerKeyRef = useRef('');
  const loadedContentKeyRef = useRef('');
  const contentLoadedRef = useRef(false);
  const contentGenerationRef = useRef(0);
  const handledSaveRequestNonceRef = useRef<number | undefined>(undefined);
  const markdownSavePromiseRef = useRef<Promise<boolean> | null>(null);
  const spreadsheetSavePromiseRef = useRef<Promise<boolean> | null>(null);
  const markdownRemoteSavedRef = useRef(true);
  const spreadsheetRemoteSavedRef = useRef(true);
  const refreshingRequestIdRef = useRef<number | null>(null);
  const externalConflictRef = useRef(externalConflict);
  const ownerIdentityRef = useRef<SessionFileOwnerIdentity>({ ownerSessionId: sessionId, ownerEpoch });
  const fileVersionRef = useRef(fileVersion);
  const markdownDraftRef = useRef(markdownDraft);
  const textContentRef = useRef(textContent);
  const spreadsheetDirtyRef = useRef(spreadsheetDirty);
  const retainedSessionDraftRef = useRef(retainedSessionDraft);
  ownerIdentityRef.current = { ownerSessionId: sessionId, ownerEpoch };
  fileVersionRef.current = fileVersion;
  markdownDraftRef.current = markdownDraft;
  textContentRef.current = textContent;
  spreadsheetDirtyRef.current = spreadsheetDirty;
  retainedSessionDraftRef.current = retainedSessionDraft;
  externalConflictRef.current = externalConflict;
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
  const onFileVersionLoadedRef = useRef(onFileVersionLoaded);
  onFileVersionLoadedRef.current = onFileVersionLoaded;
  const onDownloadFileRef = useRef(onDownloadFile);
  onDownloadFileRef.current = onDownloadFile;
  const onSaveMarkdownFileRef = useRef(onSaveMarkdownFile);
  onSaveMarkdownFileRef.current = onSaveMarkdownFile;
  const onSaveSpreadsheetFileRef = useRef(onSaveSpreadsheetFile);
  onSaveSpreadsheetFileRef.current = onSaveSpreadsheetFile;
  const onFileUpdatedRef = useRef(onFileUpdated);
  onFileUpdatedRef.current = onFileUpdated;

  const descriptor = useMemo(
    () => file ? resolvePreviewDescriptor(file) : null,
    [file],
  );
  const hasCustomSave = Boolean(onSaveMarkdownFile || onSaveSpreadsheetFile);
  const hasWorkspaceSave = file?.source === 'workspace' && Boolean(file.entry_id);
  const effectiveReadOnly = readOnly || Boolean(previewUrlBuilder && !hasCustomSave && !hasWorkspaceSave);
  const spreadsheetReadOnly = descriptor?.kind === 'spreadsheet' && (
    effectiveReadOnly || Boolean(retainedSessionDraft) || descriptor.type === 'xls' || descriptor.type === 'et'
  );
  const markdownHeadings = useMemo(
    () => descriptor?.kind === 'markdown' ? extractMarkdownHeadings(markdownDraft) : [],
    [descriptor?.kind, markdownDraft],
  );

  const getPreviewApiUrl = () => {
    if (!file) return '';
    const currentPreviewUrlBuilder = previewUrlBuilderRef.current;
    if (currentPreviewUrlBuilder) return currentPreviewUrlBuilder(file);
    const url = buildSandboxFileUrl(sessionId, file.path, true);
    return isSessionEditable ? `${url}&edit=true` : url;
  };

  const isSessionEditable = !effectiveReadOnly && !hasCustomSave && !previewUrlBuilder
    && file?.source !== 'workspace' && file?.content_mode !== 'captured'
    && (descriptor?.kind === 'markdown' || (descriptor?.kind === 'spreadsheet' && !spreadsheetReadOnly));

  const sessionContentVersion = (response: Response): FileInfo | null => {
    if (!file || !isSessionEditable) return file;
    const token = response.headers?.get('X-Session-Edit-Base');
    const revision = response.headers?.get('X-Session-File-Revision');
    if (!token || !revision) return file;
    return { ...file, source: 'session', session_id: sessionId, edit_base_token: token, revision,
      size: Number(response.headers.get('Content-Length')),
      modified: response.headers.get('X-Session-File-Modified') || file.modified };
  };

  const getPreviewCacheKey = (suffix = '') => {
    if (!file) return '';
    return [
      getPreviewApiUrl(),
      file.content_mode || 'current',
      file.source || 'session',
      file.entry_id || '',
      file.version_id || '',
      file.snapshot_path || '',
      file.assistant_ref_id || '',
      String(file.revision ?? ''),
      file.modified,
      String(file.size),
      `reload:${localReloadNonce}`,
      suffix,
    ].join('::');
  };

  const readCachedPreview = (key: string): PreviewCacheEntry | null => (
    readFilePreviewCache<PreviewCacheEntry>(key)
  );

  const writeCachedPreview = (key: string, value: PreviewCacheEntry): void => {
    const currentFile = fileRef.current;
    writeFilePreviewCache(
      key,
      value,
      currentFile?.source === 'workspace' ? currentFile.entry_id : undefined,
    );
  };

  const getApplicableRetainedDraft = async (
    kind: 'markdown' | 'spreadsheet', loadedFile = file, content?: string | ArrayBuffer,
  ) => {
    if (!file || effectiveReadOnly || file.content_mode === 'captured') return null;
    if (file.source === 'workspace' && file.entry_id) {
      const draft = await getWorkspaceDraft(file.entry_id);
      return draft?.kind === kind ? draft : null;
    }
    if (
      (kind === 'markdown' && onSaveMarkdownFileRef.current)
      || (kind === 'spreadsheet' && onSaveSpreadsheetFileRef.current)
    ) return null;
    const draft = loadedFile && content !== undefined
      ? await resolveSessionDraftForRead(sessionId, loadedFile, content)
      : await getSessionDraft(sessionId, file.path);
    if (!draft || draft.kind !== kind) {
      retainedSessionDraftRef.current = null;
      setRetainedSessionDraft(null);
      return null;
    }
    if (draft.status === 'conflict' || draft.status === 'retained') {
      retainedSessionDraftRef.current = draft;
      setRetainedSessionDraft(draft);
      return null;
    }
    retainedSessionDraftRef.current = null;
    setRetainedSessionDraft(null);
    return draft;
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

  const commitLoadedVersion = (requestId: number, loadedFile = file) => {
    if (requestId !== requestIdRef.current || !loadedFile) return;
    contentLoadedRef.current = true;
    markdownRemoteSavedRef.current = true;
    spreadsheetRemoteSavedRef.current = true;
    loadedContentKeyRef.current = previewContentKey(loadedFile);
    fileVersionRef.current = loadedFile;
    setFileVersion(loadedFile);
    onFileVersionLoadedRef.current?.(loadedFile);
    refreshingRequestIdRef.current = null;
    setPreviewLoadState('ready');
  };

  const reportLoadError = (requestId: number, message: string) => {
    if (refreshingRequestIdRef.current === requestId) {
      setPreviewNotice(`${message}，旧版本仍可查看，请稍后重试。`);
      setPreviewLoadState('ready');
      return;
    }
    setError(message);
    setPreviewLoadState('error');
  };

  const acceptSessionSave = useCallback((snapshot: SessionSaveSnapshot) => {
    const active = fileRef.current;
    if (!active || effectiveReadOnly || active.content_mode === 'captured' || active.source === 'workspace'
      || snapshot.key !== sessionDraftKey(sessionId, active.path)
      || hasPendingSessionDraft(sessionId, active.path)) return;
    if (acceptedSessionSaveRef.current === snapshot) return;
    acceptedSessionSaveRef.current = snapshot;
    // All mounted views consume the same outbox acknowledgement. A response to
    // an editor that was closed must also update its newly opened replacement.
    requestIdRef.current += 1;
    const updated = { ...snapshot.file, session_id: sessionId };
    selfSavedModifiedRef.current = updated.modified;
    fileVersionRef.current = updated;
    setFileVersion(updated);
    loadedContentKeyRef.current = previewContentKey(updated);
    if (typeof snapshot.content === 'string') {
      textContentRef.current = snapshot.content;
      markdownDraftRef.current = snapshot.content;
      setTextContent(snapshot.content);
      setMarkdownDraft(snapshot.content);
    } else {
      if (updated.session_auto_merged || !contentLoadedRef.current) setSpreadsheetSource(snapshot.content);
      spreadsheetDirtyRef.current = false;
      setSpreadsheetDirty(false);
      spreadsheetDraftKeyRef.current = null;
      spreadsheetDraftCapturePromiseRef.current = null;
    }
    draftOutboxGenerationRef.current = snapshot.generation;
    retainedSessionDraftRef.current = null;
    setRetainedSessionDraft(null);
    markdownRemoteSavedRef.current = true;
    spreadsheetRemoteSavedRef.current = true;
    contentLoadedRef.current = true;
    setPreviewLoadState('ready');
    setPreviewNotice('');
    onFileUpdatedRef.current?.(updated);
  }, [effectiveReadOnly, sessionId]);

  useEffect(() => subscribeSessionSaves(acceptSessionSave), [acceptSessionSave]);

  const acceptWorkspaceDraftLoss = useCallback((notice: WorkspaceDraftLossNotice) => {
    const active = fileRef.current;
    if (
      !active
      || active.source !== 'workspace'
      || active.entry_id !== notice.entryId
    ) return;
    takeWorkspaceDraftLossNotice(notice.entryId);
    loadedPreviewOwnerKeyRef.current = '';
    loadedContentKeyRef.current = '';
    contentLoadedRef.current = false;
    markdownRemoteSavedRef.current = true;
    spreadsheetRemoteSavedRef.current = true;
    spreadsheetDraftKeyRef.current = null;
    spreadsheetDraftCapturePromiseRef.current = null;
    spreadsheetDirtyRef.current = false;
    setSpreadsheetDirty(false);
    setMarkdownSaveError('');
    setSpreadsheetSaveError('');
    setWorkspaceDraftLossNotice(notice);
    setLocalReloadNonce((value) => value + 1);
  }, []);

  useEffect(
    () => subscribeWorkspaceDraftLosses(acceptWorkspaceDraftLoss),
    [acceptWorkspaceDraftLoss],
  );

  useEffect(() => {
    setWorkspaceDraftLossNotice(null);
    if (file?.source !== 'workspace' || !file.entry_id) return;
    const notice = takeWorkspaceDraftLossNotice(file.entry_id);
    if (notice) acceptWorkspaceDraftLoss(notice);
  }, [acceptWorkspaceDraftLoss, file?.entry_id, file?.source]);

  useLayoutEffect(() => {
    if (!file) return undefined;

    const nextOwnerKey = previewOwnerKey(sessionId, ownerEpoch, file);
    const nextContentKey = previewContentKey(file);
    const nextDescriptor = resolvePreviewDescriptor(file);
    const sameOwner = loadedPreviewOwnerKeyRef.current === nextOwnerKey;
    if (
      sameOwner
      && selfSavedModifiedRef.current
      && selfSavedModifiedRef.current === file.modified
    ) {
      selfSavedModifiedRef.current = '';
      loadedContentKeyRef.current = nextContentKey;
      setFileVersion(file);
      return undefined;
    }
    const loadedVersion = fileVersionRef.current;
    const metadataOnlyUpdate = Boolean(
      refreshInPlace
      && contentLoadedRef.current
      && sameOwner
      && loadedVersion?.entry_id
      && loadedVersion.entry_id === file.entry_id
      && (
        loadedContentKeyRef.current === nextContentKey
        || Boolean(loadedVersion.version_id && loadedVersion.version_id === file.version_id)
      ),
    );
    if (metadataOnlyUpdate) {
      // 持久工作区的重命名/移动会推进 entry revision，但内容版本不变。
      // 只更新路径和版本元数据，保留已加载内容以及未保存草稿。
      loadedPreviewOwnerKeyRef.current = nextOwnerKey;
      loadedContentKeyRef.current = nextContentKey;
      fileVersionRef.current = file;
      setFileVersion(file);
      onFileVersionLoadedRef.current?.(file);
      return undefined;
    }
    const hasLocalChanges = (
      nextDescriptor.kind === 'markdown'
        ? (markdownEditorRef.current?.getMarkdown() ?? markdownDraftRef.current) !== textContentRef.current
        : nextDescriptor.kind === 'spreadsheet' && spreadsheetDirtyRef.current
    );
    if (refreshInPlace && sameOwner && contentLoadedRef.current && hasLocalChanges) {
      setPreviewNotice('文件已有新版本；当前草稿已保留，完成保存后会继续同步。');
      return undefined;
    }
    loadedPreviewOwnerKeyRef.current = nextOwnerKey;
    if (!sameOwner) {
      loadedContentKeyRef.current = '';
      retainedSessionDraftRef.current = null;
      setRetainedSessionDraft(null);
    }
    contentGenerationRef.current += 1;

    requestIdRef.current += 1;
    const requestId = requestIdRef.current;
    const controller = new AbortController();
    const shouldRefreshInPlace = refreshInPlace && sameOwner && contentLoadedRef.current;
    refreshingRequestIdRef.current = shouldRefreshInPlace ? requestId : null;
    setPresentationRequestId(nextDescriptor.kind === 'presentation' ? requestId : 0);

    setError('');
    setPreviewNotice('');
    if (!shouldRefreshInPlace) {
      fileVersionRef.current = file;
      setFileVersion(file);
    }
    setSavingMarkdown(false);
    setMarkdownSaveError('');
    setSavingSpreadsheet(false);
    setSpreadsheetSaveError('');
    setOfficePreviewSlow(false);
    setPreviewLoadState(shouldRefreshInPlace ? 'refreshing' : 'initial-loading');
    if (!shouldRefreshInPlace) {
      contentLoadedRef.current = false;
      setTextContent('');
      setMarkdownDraft('');
      textContentRef.current = '';
      markdownDraftRef.current = '';
      markdownRevisionRef.current = 0;
      setMarkdownRevision(0);
      setMarkdownToolbarOpen(false);
      setMarkdownTocOpen(false);
      setDocxHtml('');
      setSpreadsheetSource(null);
      setSpreadsheetDirty(false);
      spreadsheetDirtyRef.current = false;
      spreadsheetRevisionRef.current = 0;
      draftOutboxGenerationRef.current = 0;
      spreadsheetDraftKeyRef.current = null;
      spreadsheetDraftCapturePromiseRef.current = null;
      setSpreadsheetRevision(0);
      setZipEntries([]);
      setZipQuery('');
      setBinaryPreviewUrl('');
      setConvertedPdfUrl('');
      setPresentationSource(null);
      setHtmlFrameLoading(false);
      setViewMode('rendered');
      setWrapLongLines(false);
      setImageScale(1);
      setImageRotation(0);
    }

    const startLoader = () => {
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
          commitLoadedVersion(requestId);
          break;
      }
    };
    startLoader();

    return () => controller.abort();
  // Loader 只由文件版本和 owner 身份驱动；URL builder 通过 ref 读取，父级流式重渲染不得重载预览。
  // requestIdRef 额外丢弃不遵守 abort 的迟到响应。
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [file?.assistant_ref_id, file?.content_mode, file?.entry_id, file?.modified, file?.name, file?.path, file?.revision, file?.size, file?.snapshot_path, file?.source, file?.type, file?.version_id, refreshInPlace, localReloadNonce, reloadNonce, ownerEpoch, sessionId]);

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
    const isInitialOfficeLoad = previewLoadState === 'initial-loading'
      && (descriptor?.kind === 'document' || descriptor?.kind === 'presentation')
      && (descriptor.kind !== 'presentation' || !hasCurrentPresentationSource)
      && (descriptor.kind !== 'document' || !convertedPdfUrl);
    if (!isInitialOfficeLoad) {
      setOfficePreviewSlow(false);
      return undefined;
    }
    const timer = window.setTimeout(() => setOfficePreviewSlow(true), OFFICE_PREVIEW_SLOW_HINT_MS);
    return () => window.clearTimeout(timer);
  }, [convertedPdfUrl, descriptor?.kind, hasCurrentPresentationSource, previewLoadState]);

  useEffect(() => {
    const viewport = contentViewportRef.current;
    if (!viewport) return;
    if (backgroundRefreshing && loading) {
      viewport.setAttribute('inert', '');
      viewport.setAttribute('aria-busy', 'true');
    } else {
      viewport.removeAttribute('inert');
      viewport.removeAttribute('aria-busy');
    }
  }, [backgroundRefreshing, loading]);

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
    setError('');
    try {
      const cacheKey = getPreviewCacheKey();
      const cached = cacheKey && !isSessionEditable ? readCachedPreview(cacheKey) : null;
      if (cached?.kind === 'text') {
        if (requestId === requestIdRef.current) {
          const retainedDraft = file && resolvePreviewDescriptor(file).kind === 'markdown'
            ? await getApplicableRetainedDraft('markdown')
            : null;
          if (requestId !== requestIdRef.current) return;
          setTextContent(cached.text);
          textContentRef.current = cached.text;
          const loadedMarkdown = retainedDraft?.kind === 'markdown'
            ? retainedDraft.content as string
            : cached.text;
          setMarkdownDraft(loadedMarkdown);
          markdownDraftRef.current = loadedMarkdown;
          commitLoadedVersion(requestId);
          if (retainedDraft?.kind === 'markdown') {
            fileVersionRef.current = retainedDraft.file;
            setFileVersion(retainedDraft.file);
            draftOutboxGenerationRef.current = retainedDraft.generation;
            markdownRevisionRef.current += 1;
            setMarkdownRevision(markdownRevisionRef.current);
          }
        }
        return;
      }

      const response = await fetchPreviewResponse(getPreviewApiUrl(), signal);
      const text = await response.text();
      const loadedFile = sessionContentVersion(response);
      if (requestId !== requestIdRef.current) return;
      if (file && resolvePreviewDescriptor(file).kind === 'html') {
        setHtmlFrameLoading(true);
      }
      const retainedDraft = file && resolvePreviewDescriptor(file).kind === 'markdown'
        ? await getApplicableRetainedDraft('markdown', loadedFile, text)
        : null;
      if (requestId !== requestIdRef.current) return;
      setTextContent(text);
      textContentRef.current = text;
      const loadedMarkdown = retainedDraft?.kind === 'markdown'
        ? retainedDraft.content as string
        : text;
      setMarkdownDraft(loadedMarkdown);
      markdownDraftRef.current = loadedMarkdown;
      commitLoadedVersion(requestId, loadedFile);
      if (retainedDraft?.kind === 'markdown') {
        fileVersionRef.current = retainedDraft.file;
        setFileVersion(retainedDraft.file);
        draftOutboxGenerationRef.current = retainedDraft.generation;
        spreadsheetDraftKeyRef.current = retainedDraft.key;
        markdownRevisionRef.current += 1;
        setMarkdownRevision(markdownRevisionRef.current);
      }
      if (cacheKey) writeCachedPreview(cacheKey, { kind: 'text', text });
    } catch (err) {
      if (requestId !== requestIdRef.current || isAbortError(err)) return;
      console.error('Failed to load text content:', err);
      reportLoadError(requestId, '加载文件内容失败');
    }
  };

  const loadDocxFallback = async (requestId: number, signal: AbortSignal) => {
    const cacheKey = getPreviewCacheKey('::mammoth');
    const cached = cacheKey ? readCachedPreview(cacheKey) : null;
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
    if (cacheKey) writeCachedPreview(cacheKey, { kind: 'docx', html: sanitizedHtml });
    if (result.messages.length > 0) console.warn('DOCX conversion warnings:', result.messages);
  };

  const loadOfficePreview = async (
    requestId: number,
    signal: AbortSignal,
    officeDescriptor: PreviewDescriptor,
  ) => {
    setError('');
    try {
      const pdfCacheKey = getPreviewCacheKey('::render=pdf');
      const cached = pdfCacheKey ? readCachedPreview(pdfCacheKey) : null;
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
        if (pdfCacheKey) writeCachedPreview(pdfCacheKey, { kind: 'binary', blob: pdfBlob });
      }
      if (requestId !== requestIdRef.current) return;
      if (officeDescriptor.kind === 'presentation') {
        setPresentationSource({
          blob: pdfBlob,
          key: `${pdfCacheKey || 'presentation'}::${requestId}`,
          requestId,
        });
      } else {
        setConvertedPdfUrl(URL.createObjectURL(pdfBlob));
        commitLoadedVersion(requestId);
      }
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
          commitLoadedVersion(requestId);
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

    if (requestId === requestIdRef.current) {
      setPreviewLoadState(
        refreshingRequestIdRef.current === requestId && contentLoadedRef.current
          ? 'ready'
          : 'error',
      );
    }
  };

  const handlePresentationReady = (sourceKey: string) => {
    const source = presentationSource;
    if (
      !source
      || source.key !== sourceKey
      || source.requestId !== presentationRequestId
      || source.requestId !== requestIdRef.current
    ) return;
    commitLoadedVersion(source.requestId);
  };

  const handlePresentationError = (sourceKey: string) => {
    const source = presentationSource;
    if (
      !source
      || source.key !== sourceKey
      || source.requestId !== presentationRequestId
      || source.requestId !== requestIdRef.current
    ) return;
    if (refreshingRequestIdRef.current === source.requestId && contentLoadedRef.current) {
      reportLoadError(source.requestId, '更新幻灯片预览失败');
      return;
    }
    setPreviewNotice('高级幻灯片浏览器加载失败，已切换为浏览器 PDF 预览。');
    commitLoadedVersion(source.requestId);
  };

  const loadSpreadsheetContent = async (requestId: number, signal: AbortSignal) => {
    setError('');
    try {
      const response = await fetchPreviewResponse(getPreviewApiUrl(), signal);
      const arrayBuffer = await response.arrayBuffer();
      const loadedFile = sessionContentVersion(response);
      if (requestId !== requestIdRef.current) return;
      const retainedDraft = file
        ? await getApplicableRetainedDraft('spreadsheet', loadedFile, arrayBuffer)
        : null;
      if (requestId !== requestIdRef.current) return;
      const loadedSpreadsheet = retainedDraft?.kind === 'spreadsheet'
        ? retainedDraft.content as ArrayBuffer
        : arrayBuffer;
      setSpreadsheetSource(loadedSpreadsheet);
      commitLoadedVersion(requestId, loadedFile);
      if (retainedDraft?.kind === 'spreadsheet') {
        fileVersionRef.current = retainedDraft.file;
        setFileVersion(retainedDraft.file);
        draftOutboxGenerationRef.current = retainedDraft.generation;
        spreadsheetRevisionRef.current += 1;
        spreadsheetDirtyRef.current = true;
        setSpreadsheetRevision(spreadsheetRevisionRef.current);
        setSpreadsheetDirty(true);
      }
    } catch (err) {
      if (requestId !== requestIdRef.current || isAbortError(err)) return;
      console.error('Failed to load spreadsheet content:', err);
      reportLoadError(requestId, '加载电子表格失败');
    }
  };

  const loadZipContent = async (requestId: number, signal: AbortSignal) => {
    setError('');
    try {
      if (file && file.size > MAX_ZIP_PREVIEW_BYTES) {
        throw new Error('ZIP 文件超过 10 MB，请下载后解压查看');
      }
      const cacheKey = getPreviewCacheKey();
      const cached = cacheKey ? readCachedPreview(cacheKey) : null;
      if (cached?.kind === 'zip') {
        if (requestId === requestIdRef.current) {
          setZipEntries(cached.entries);
          commitLoadedVersion(requestId);
        }
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
      commitLoadedVersion(requestId);
      if (cacheKey) writeCachedPreview(cacheKey, { kind: 'zip', entries });
    } catch (err) {
      if (requestId !== requestIdRef.current || isAbortError(err)) return;
      console.error('Failed to load ZIP directory:', err);
      reportLoadError(requestId, err instanceof Error ? err.message : '加载 ZIP 目录失败');
    }
  };

  const loadBinaryPreview = async (requestId: number, signal: AbortSignal) => {
    setError('');
    try {
      const cacheKey = getPreviewCacheKey();
      const cached = cacheKey ? readCachedPreview(cacheKey) : null;
      let blob: Blob;
      if (cached?.kind === 'binary') {
        blob = cached.blob;
      } else {
        const response = await fetchPreviewResponse(getPreviewApiUrl(), signal);
        blob = await response.blob();
        if (cacheKey) writeCachedPreview(cacheKey, { kind: 'binary', blob });
      }
      if (requestId !== requestIdRef.current) return;
      setBinaryPreviewUrl(URL.createObjectURL(blob));
      commitLoadedVersion(requestId);
    } catch (err) {
      if (requestId !== requestIdRef.current || isAbortError(err)) return;
      console.error('Failed to load binary content:', err);
      reportLoadError(requestId, '加载文件预览失败');
    }
  };

  const handleDownload = async () => {
    if (!file) return;
    try {
      if (onDownloadFileRef.current) await onDownloadFileRef.current(file);
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

  const surfaceRetainedSessionDraft = useCallback(async (
    saveError: unknown,
    activeFile: FileInfo,
  ): Promise<boolean> => {
    const retained = await getSessionDraft(sessionId, activeFile.path);
    if (!retained || (retained.status !== 'conflict' && retained.status !== 'retained')) return false;
    retainedSessionDraftRef.current = retained;
    setRetainedSessionDraft(retained);

    const current = conflictCurrentFile(saveError);
    if (current) {
      const authoritative = {
        ...activeFile,
        ...current,
        session_id: activeFile.session_id || sessionId,
      } as FileInfo;
      fileVersionRef.current = authoritative;
      setFileVersion(authoritative);
      onFileUpdatedRef.current?.(authoritative);
    }
    // 使用新的 cache identity 重新读取权威字节；retained 草稿只留作下载或丢弃。
    setLocalReloadNonce((value) => value + 1);
    onSaveFailureRef.current?.();
    return true;
  }, [sessionId]);

  const handleDownloadRetainedDraft = useCallback(() => {
    const retained = retainedSessionDraftRef.current;
    if (!retained) return;
    const blob = new Blob(
      [retained.content],
      { type: retained.kind === 'markdown' ? 'text/markdown;charset=utf-8' : 'application/octet-stream' },
    );
    const objectUrl = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = objectUrl;
    link.download = `${retained.file.name || fileRef.current?.name || 'session-file'}.draft`;
    document.body.appendChild(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(objectUrl);
  }, []);

  const handleDiscardRetainedDraft = useCallback(async () => {
    const retained = retainedSessionDraftRef.current;
    if (!retained) return;
    try {
      await discardSessionDraft(retained.sessionId, retained.path);
      if (retainedSessionDraftRef.current?.key !== retained.key) return;
      retainedSessionDraftRef.current = null;
      setRetainedSessionDraft(null);
      spreadsheetDraftKeyRef.current = null;
      spreadsheetDraftCapturePromiseRef.current = null;
      spreadsheetDirtyRef.current = false;
      setSpreadsheetDirty(false);
      setLocalReloadNonce((value) => value + 1);
      onSaveFailureRef.current?.();
    } catch (discardError) {
      console.error('Failed to discard retained Session draft:', discardError);
      setError('丢弃本地草稿失败，请稍后重试');
    }
  }, []);

  const handleSaveMarkdown = useCallback((): Promise<boolean> => {
    // Capture before returning an existing save promise: the editor may have
    // newer input than the request already in flight.
    const captured = markdownEditorRef.current?.getMarkdown() ?? markdownDraftRef.current;
    const captureFile = fileVersionRef.current;
    if (!effectiveReadOnly && !retainedSessionDraftRef.current && captureFile
      && captureFile.source !== 'workspace' && !onSaveMarkdownFileRef.current
      && descriptor?.kind === 'markdown' && captured !== textContentRef.current) {
      markdownDraftRef.current = captured;
      setMarkdownDraft(captured);
      const queued = queueSessionMarkdownDraft(sessionId, captureFile, captured);
      draftOutboxGenerationRef.current = queued.generation;
    }
    if (markdownSavePromiseRef.current) return markdownSavePromiseRef.current;
    if (externalConflictRef.current || retainedSessionDraftRef.current) return Promise.resolve(false);
    const activeFile = fileRef.current;
    if (
      !activeFile
      || descriptor?.kind !== 'markdown'
      || effectiveReadOnly
    ) return Promise.resolve(true);

    const savePromise = (async () => {
      const contentGeneration = contentGenerationRef.current;
      setSavingMarkdown(true);
      setMarkdownSaveError('');
      try {
        while (true) {
          const currentMarkdown = markdownEditorRef.current?.getMarkdown() ?? markdownDraftRef.current;
          if (currentMarkdown === textContentRef.current) {
            markdownRemoteSavedRef.current = true;
            setMarkdownSaveError('');
            return true;
          }

          const currentVersion = fileVersionRef.current;
          if (!currentVersion) return false;
          const revisionAtStart = markdownRevisionRef.current;
          const currentSaveMarkdown = onSaveMarkdownFileRef.current;
          let updated: FileInfo;
          if (currentVersion.source === 'workspace') {
            const queued = queueWorkspaceMarkdownDraft(currentVersion, currentMarkdown);
            draftOutboxGenerationRef.current = queued.generation;
            updated = await flushWorkspaceDraft(queued.key);
          } else if (currentSaveMarkdown) {
            updated = await currentSaveMarkdown(currentVersion, currentMarkdown);
          } else {
            const queued = queueSessionMarkdownDraft(sessionId, currentVersion, currentMarkdown);
            draftOutboxGenerationRef.current = queued.generation;
            updated = await flushSessionDraft(queued.key);
          }
          if (contentGeneration !== contentGenerationRef.current) return true;
          if (currentVersion.source !== 'workspace' && !currentSaveMarkdown) {
            const snapshot = getSessionSaveSnapshot(sessionId, currentVersion.path);
            if (hasPendingSessionDraft(sessionId, currentVersion.path)) continue;
            if (snapshot) acceptSessionSave(snapshot);
            return true;
          }
          markdownRemoteSavedRef.current = true;
          let mergedMarkdown: string | undefined;
          if (currentVersion.source === 'workspace' && updated.workspace_auto_merged) {
            const mergeReadRevision = markdownRevisionRef.current;
            mergedMarkdown = await (await fetchMergedWorkspaceVersion(updated)).text();
            const pending = await getWorkspaceDraft(updated.entry_id!);
            if (contentGeneration !== contentGenerationRef.current) return true;
            // 读取期间仍允许输入；迟到正文不能覆盖新稿，也不能改变其基线。
            if (mergeReadRevision !== markdownRevisionRef.current || pending) continue;
          }
          const updatedWithSession = currentVersion.source === 'workspace' || currentSaveMarkdown
            ? updated
            : { ...updated, session_id: activeFile.session_id || sessionId };
          selfSavedModifiedRef.current = updated.modified;
          fileVersionRef.current = updatedWithSession;
          setFileVersion(updatedWithSession);
          onFileUpdatedRef.current?.(updatedWithSession);

          if (mergedMarkdown !== undefined) {
            textContentRef.current = mergedMarkdown;
            markdownDraftRef.current = mergedMarkdown;
            setTextContent(mergedMarkdown);
            setMarkdownDraft(mergedMarkdown);
            loadedContentKeyRef.current = previewContentKey(updatedWithSession);
            return true;
          }

          if (updated.outbox_generation !== undefined) {
            if (updated.outbox_generation === draftOutboxGenerationRef.current) {
              const latestMarkdown = markdownEditorRef.current?.getMarkdown() ?? markdownDraftRef.current;
              textContentRef.current = latestMarkdown;
              markdownDraftRef.current = latestMarkdown;
              setTextContent(latestMarkdown);
              setMarkdownDraft(latestMarkdown);
              return true;
            }
            continue;
          }

          textContentRef.current = currentMarkdown;
          setTextContent(currentMarkdown);

          if (revisionAtStart === markdownRevisionRef.current) {
            markdownDraftRef.current = currentMarkdown;
            setMarkdownDraft(currentMarkdown);
            return true;
          }
          // 用户在保存期间继续输入：立即用刚返回的新版本令牌串行保存最新草稿。
        }
      } catch (err) {
        if (contentGeneration !== contentGenerationRef.current) return true;
        markdownRemoteSavedRef.current = false;
        if (!isExpectedWorkspaceSaveWait(err)) {
          console.error('Failed to save Markdown file:', err);
        }
        if (
          activeFile.source !== 'workspace'
          && !onSaveMarkdownFileRef.current
          && await surfaceRetainedSessionDraft(err, activeFile)
        ) {
          setMarkdownSaveError('');
          return false;
        }
        if (!onSaveMarkdownFileRef.current || activeFile.source === 'workspace') {
          // Workspace 草稿由页面内 outbox 决定重试或丢弃；两者都不阻止用户操作。
          setMarkdownSaveError('');
          return true;
        }
        setMarkdownSaveError(err instanceof Error ? err.message : 'Markdown 保存失败');
        onSaveFailureRef.current?.();
        return false;
      } finally {
        setSavingMarkdown(false);
      }
    })();
    markdownSavePromiseRef.current = savePromise;
    const clearSavePromise = () => {
      if (markdownSavePromiseRef.current === savePromise) markdownSavePromiseRef.current = null;
    };
    void savePromise.then(clearSavePromise, clearSavePromise);
    return savePromise;
  }, [
    descriptor?.kind,
    effectiveReadOnly,
    sessionId,
    surfaceRetainedSessionDraft,
    acceptSessionSave,
  ]);

  useEffect(() => {
    if (
      descriptor?.kind !== 'markdown'
      || effectiveReadOnly
      || !markdownDirty
      || savingMarkdown
      || markdownSaveError
      || retainedSessionDraft
      || externalConflict
      || backgroundRefreshing
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
    retainedSessionDraft,
    effectiveReadOnly,
    savingMarkdown,
    externalConflict,
    backgroundRefreshing,
  ]);

  const markSpreadsheetDirty = useCallback(() => {
    spreadsheetRevisionRef.current += 1;
    const captureRevision = spreadsheetRevisionRef.current;
    spreadsheetDirtyRef.current = true;
    spreadsheetRemoteSavedRef.current = false;
    setSpreadsheetRevision(spreadsheetRevisionRef.current);
    setSpreadsheetDirty(true);
    setSpreadsheetSaveError('');
    const currentFile = fileVersionRef.current;
    if (!currentFile) return;
    const editor = spreadsheetEditorRef.current;
    if (!editor) return;
    const capture = editor.exportFileDeferred
      ? editor.exportFileDeferred()
      : Promise.resolve().then(() => editor.exportFile());
    const captureTask = capture.then((content) => {
      if (captureRevision !== spreadsheetRevisionRef.current) return;
      if (currentFile.source === 'workspace') {
        const queued = queueWorkspaceSpreadsheetDraft(currentFile, content);
        draftOutboxGenerationRef.current = queued.generation;
        spreadsheetDraftKeyRef.current = queued.key;
      } else if (!onSaveSpreadsheetFileRef.current) {
        const queued = queueSessionSpreadsheetDraft(sessionId, currentFile, content);
        draftOutboxGenerationRef.current = queued.generation;
        spreadsheetDraftKeyRef.current = queued.key;
      }
    });
    spreadsheetDraftCapturePromiseRef.current = captureTask;
    void captureTask.catch((error) => {
      console.error('Failed to capture spreadsheet draft:', error);
    });
  }, [sessionId]);

  const handleSaveSpreadsheet = useCallback((): Promise<boolean> => {
    if (spreadsheetSavePromiseRef.current) return spreadsheetSavePromiseRef.current;
    if (externalConflictRef.current || retainedSessionDraftRef.current) return Promise.resolve(false);
    const activeFile = fileRef.current;
    if (
      !activeFile
      || descriptor?.kind !== 'spreadsheet'
      || spreadsheetReadOnly
    ) return Promise.resolve(true);

    const savePromise = (async () => {
      const contentGeneration = contentGenerationRef.current;
      setSavingSpreadsheet(true);
      setSpreadsheetSaveError('');
      try {
        while (spreadsheetDirtyRef.current) {
          const currentVersion = fileVersionRef.current;
          if (!currentVersion) return false;
          const revisionAtStart = spreadsheetRevisionRef.current;
          const currentSaveSpreadsheet = onSaveSpreadsheetFileRef.current;
          let updated: FileInfo;
          if (spreadsheetDraftCapturePromiseRef.current) {
            await spreadsheetDraftCapturePromiseRef.current;
          }
          const capturedDraftKey = spreadsheetDraftKeyRef.current;
          if (currentVersion.source === 'workspace' && capturedDraftKey) {
            updated = await flushWorkspaceDraft(capturedDraftKey);
          } else if (!currentSaveSpreadsheet && capturedDraftKey) {
            updated = await flushSessionDraft(capturedDraftKey);
          } else {
            const content = spreadsheetEditorRef.current?.exportFile();
            if (!content) throw new Error('电子表格编辑器尚未准备好');
            if (currentVersion.source === 'workspace') {
              const queued = queueWorkspaceSpreadsheetDraft(currentVersion, content);
              draftOutboxGenerationRef.current = queued.generation;
              spreadsheetDraftKeyRef.current = queued.key;
              updated = await flushWorkspaceDraft(queued.key);
            } else if (currentSaveSpreadsheet) {
              updated = await currentSaveSpreadsheet(currentVersion, content);
            } else {
              const queued = queueSessionSpreadsheetDraft(sessionId, currentVersion, content);
              draftOutboxGenerationRef.current = queued.generation;
              spreadsheetDraftKeyRef.current = queued.key;
              updated = await flushSessionDraft(queued.key);
            }
          }
          if (contentGeneration !== contentGenerationRef.current) return true;
          if (currentVersion.source !== 'workspace' && !currentSaveSpreadsheet) {
            const snapshot = getSessionSaveSnapshot(sessionId, currentVersion.path);
            if (hasPendingSessionDraft(sessionId, currentVersion.path)) continue;
            if (snapshot) acceptSessionSave(snapshot);
            return true;
          }
          spreadsheetRemoteSavedRef.current = true;
          let mergedSpreadsheet: ArrayBuffer | undefined;
          if (currentVersion.source === 'workspace' && updated.workspace_auto_merged) {
            const mergeReadRevision = spreadsheetRevisionRef.current;
            mergedSpreadsheet = await (await fetchMergedWorkspaceVersion(updated)).arrayBuffer();
            const pending = await getWorkspaceDraft(updated.entry_id!);
            if (contentGeneration !== contentGenerationRef.current) return true;
            if (mergeReadRevision !== spreadsheetRevisionRef.current || pending) continue;
          }
          const updatedWithSession = currentVersion.source === 'workspace' || currentSaveSpreadsheet
            ? updated
            : { ...updated, session_id: activeFile.session_id || sessionId };
          selfSavedModifiedRef.current = updated.modified;
          fileVersionRef.current = updatedWithSession;
          setFileVersion(updatedWithSession);
          onFileUpdatedRef.current?.(updatedWithSession);
          if (mergedSpreadsheet !== undefined) {
            setSpreadsheetSource(mergedSpreadsheet);
            spreadsheetDraftKeyRef.current = null;
            spreadsheetDraftCapturePromiseRef.current = null;
            spreadsheetDirtyRef.current = false;
            setSpreadsheetDirty(false);
            loadedContentKeyRef.current = previewContentKey(updatedWithSession);
            return true;
          }
          if (updated.outbox_generation !== undefined) {
            if (updated.outbox_generation === draftOutboxGenerationRef.current) {
              spreadsheetDraftKeyRef.current = null;
              spreadsheetDraftCapturePromiseRef.current = null;
              spreadsheetDirtyRef.current = false;
              setSpreadsheetDirty(false);
              return true;
            }
            continue;
          }
          if (revisionAtStart === spreadsheetRevisionRef.current) {
            spreadsheetDirtyRef.current = false;
            setSpreadsheetDirty(false);
            return true;
          }
        }
        return true;
      } catch (err) {
        if (contentGeneration !== contentGenerationRef.current) return true;
        spreadsheetRemoteSavedRef.current = false;
        if (!isExpectedWorkspaceSaveWait(err)) {
          console.error('Failed to save spreadsheet file:', err);
        }
        if (
          activeFile.source !== 'workspace'
          && !onSaveSpreadsheetFileRef.current
          && await surfaceRetainedSessionDraft(err, activeFile)
        ) {
          setSpreadsheetSaveError('');
          return false;
        }
        if (!onSaveSpreadsheetFileRef.current || activeFile.source === 'workspace') {
          // Workspace 草稿由页面内 outbox 决定重试或丢弃；两者都不阻止用户操作。
          setSpreadsheetSaveError('');
          return true;
        }
        setSpreadsheetSaveError(err instanceof Error ? err.message : '电子表格保存失败');
        onSaveFailureRef.current?.();
        return false;
      } finally {
        setSavingSpreadsheet(false);
      }
    })();
    spreadsheetSavePromiseRef.current = savePromise;
    const clearSavePromise = () => {
      if (spreadsheetSavePromiseRef.current === savePromise) spreadsheetSavePromiseRef.current = null;
    };
    void savePromise.then(clearSavePromise, clearSavePromise);
    return savePromise;
  }, [descriptor?.kind, sessionId, spreadsheetReadOnly, surfaceRetainedSessionDraft, acceptSessionSave]);

  useEffect(() => {
    if (
      descriptor?.kind !== 'spreadsheet'
      || spreadsheetReadOnly
      || !spreadsheetDirty
      || savingSpreadsheet
      || spreadsheetSaveError
      || retainedSessionDraft
      || externalConflict
      || backgroundRefreshing
    ) return undefined;
    const timer = window.setTimeout(() => {
      void handleSaveSpreadsheet();
    }, SPREADSHEET_AUTOSAVE_DELAY_MS);
    return () => window.clearTimeout(timer);
  }, [
    descriptor?.kind,
    handleSaveSpreadsheet,
    savingSpreadsheet,
    spreadsheetDirty,
    spreadsheetRevision,
    spreadsheetSaveError,
    retainedSessionDraft,
    spreadsheetReadOnly,
    externalConflict,
    backgroundRefreshing,
  ]);

  useEffect(() => {
    if (saveRequestNonce === undefined || handledSaveRequestNonceRef.current === saveRequestNonce) return;
    handledSaveRequestNonceRef.current = saveRequestNonce;
    if (effectiveReadOnly || externalConflictRef.current || retainedSessionDraftRef.current) return;
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

  const hasUnsavedMarkdown = useCallback(() => (
    (markdownEditorRef.current?.getMarkdown() ?? markdownDraftRef.current) !== textContentRef.current
  ), []);

  useImperativeHandle(ref, () => ({
    ownerSessionId: sessionId,
    ownerEpoch,
    path: file?.path || '',
    isDirty: (expectedOwner) => (
      expectedOwner.ownerSessionId === sessionId
      && expectedOwner.ownerEpoch === ownerEpoch
      && (
        Boolean(retainedSessionDraftRef.current)
        || (
          !effectiveReadOnly
          && (
            (descriptor?.kind === 'markdown' && hasUnsavedMarkdown())
            || (descriptor?.kind === 'spreadsheet' && spreadsheetDirtyRef.current)
          )
        )
      )
    ),
    saveDirty: async (expectedOwner, options) => {
      const path = file?.path || '';
      const expectedMatches = expectedOwner.ownerSessionId === sessionId
        && expectedOwner.ownerEpoch === ownerEpoch;
      if (!expectedMatches) {
        return { ...expectedOwner, path, ok: false, stale: true };
      }
      if (externalConflictRef.current) {
        return { ...expectedOwner, path, ok: false, stale: true };
      }
      if (retainedSessionDraftRef.current) {
        return { ...expectedOwner, path, ok: false, stale: false };
      }
      if (effectiveReadOnly) {
        return { ...expectedOwner, path, ok: true, stale: false };
      }

      let ok = true;
      const currentMarkdownDirty = descriptor?.kind === 'markdown'
        && hasUnsavedMarkdown();
      if (currentMarkdownDirty) {
        ok = await handleSaveMarkdown();
        if (options?.requireRemote && !markdownRemoteSavedRef.current) ok = false;
      } else if (descriptor?.kind === 'spreadsheet' && spreadsheetDirtyRef.current) {
        ok = await handleSaveSpreadsheet();
        if (options?.requireRemote && !spreadsheetRemoteSavedRef.current) ok = false;
      }
      const latestWorkspaceFile = fileVersionRef.current;
      if (ok && latestWorkspaceFile?.source === 'workspace') {
        try {
          await checkpointWorkspaceFile(latestWorkspaceFile, 'web_close');
        } catch {
          // The durable head is already saved; the outbox retries checkpoint promotion.
        }
      }

      const currentOwner = ownerIdentityRef.current;
      const stale = currentOwner.ownerSessionId !== expectedOwner.ownerSessionId
        || currentOwner.ownerEpoch !== expectedOwner.ownerEpoch;
      return { ...expectedOwner, path, ok: ok && !stale, stale };
    },
    downloadDraft: (expectedOwner) => {
      const currentFile = fileRef.current;
      if (
        expectedOwner.ownerSessionId !== sessionId
        || expectedOwner.ownerEpoch !== ownerEpoch
        || !currentFile
      ) return false;
      if (retainedSessionDraftRef.current) {
        handleDownloadRetainedDraft();
        return true;
      }
      let blob: Blob | null = null;
      if (descriptor?.kind === 'markdown') {
        blob = new Blob(
          [markdownEditorRef.current?.getMarkdown() ?? markdownDraftRef.current],
          { type: 'text/markdown;charset=utf-8' },
        );
      } else if (descriptor?.kind === 'spreadsheet') {
        const content = spreadsheetEditorRef.current?.exportFile();
        if (content) blob = new Blob([content], { type: 'application/octet-stream' });
      }
      if (!blob) return false;
      const objectUrl = URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.href = objectUrl;
      link.download = `${currentFile.name}.draft`;
      document.body.appendChild(link);
      link.click();
      link.remove();
      URL.revokeObjectURL(objectUrl);
      return true;
    },
  }), [
    descriptor?.kind,
    effectiveReadOnly,
    file?.path,
    handleSaveMarkdown,
    handleSaveSpreadsheet,
    handleDownloadRetainedDraft,
    hasUnsavedMarkdown,
    ownerEpoch,
    sessionId,
  ]);

  const openWorkspacePicker = async () => {
    if (!file || workspaceImportPreparing) return;
    setWorkspaceImportPreparing(true);
    try {
      let saved = true;
      if (descriptor?.kind === 'markdown' && hasUnsavedMarkdown()) {
        saved = await handleSaveMarkdown();
      } else if (descriptor?.kind === 'spreadsheet' && spreadsheetDirtyRef.current) {
        saved = await handleSaveSpreadsheet();
      }
      if (!saved) return;
      let importVersion = fileVersionRef.current || file;
      if (!importVersion.revision) {
        const normalizedPath = importVersion.path.replace(/^\/+/, '');
        const separator = normalizedPath.lastIndexOf('/');
        const parentPath = separator >= 0 ? normalizedPath.slice(0, separator) : undefined;
        try {
          const listing = await apiService.getSessionFiles(sessionId, parentPath);
          const refreshed = listing.files.find((candidate) => candidate.path.replace(/^\/+/, '') === normalizedPath);
          if (!refreshed?.revision) {
            setError('无法确认会话文件版本，请刷新文件列表后重试。');
            return;
          }
          const changedSincePreview = (
            (Boolean(importVersion.modified) && refreshed.modified !== importVersion.modified)
            || (importVersion.size > 0 && refreshed.size !== importVersion.size)
          );
          if (changedSincePreview) {
            onFileUpdatedRef.current?.({ ...refreshed, session_id: file.session_id || sessionId });
            setError('会话文件已被更新，请刷新预览后再存入工作区。');
            return;
          }
          importVersion = { ...importVersion, ...refreshed, session_id: file.session_id || sessionId };
          fileVersionRef.current = importVersion;
          setFileVersion(importVersion);
        } catch (metadataError) {
          console.error('Failed to refresh session file revision:', metadataError);
          setError('无法刷新会话文件版本，请稍后重试。');
          return;
        }
      }
      setWorkspacePickerOpen(true);
    } finally {
      setWorkspaceImportPreparing(false);
    }
  };

  useEffect(() => {
    if (!workspaceNotice) return undefined;
    const timer = window.setTimeout(() => setWorkspaceNotice(null), DEFAULT_FEEDBACK_AUTO_DISMISS_MS);
    return () => window.clearTimeout(timer);
  }, [workspaceNotice]);

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
        if (!effectiveReadOnly && !retainedSessionDraft) {
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
                  markdownRemoteSavedRef.current = false;
                  markdownDraftRef.current = nextMarkdown;
                  setMarkdownRevision(markdownRevisionRef.current);
                  setMarkdownDraft(nextMarkdown);
                  setMarkdownSaveError('');
                  const currentFile = fileVersionRef.current;
                  if (currentFile?.source === 'workspace') {
                    const queued = queueWorkspaceMarkdownDraft(currentFile, nextMarkdown);
                    draftOutboxGenerationRef.current = queued.generation;
                  } else if (currentFile && !onSaveMarkdownFileRef.current) {
                    const queued = queueSessionMarkdownDraft(sessionId, currentFile, nextMarkdown);
                    draftOutboxGenerationRef.current = queued.generation;
                  }
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
        return presentationSource
          ? (
              <SlideDeckPreview
                source={presentationSource.blob}
                sourceKey={presentationSource.key}
                requestId={presentationSource.requestId}
                activeRequestId={presentationRequestId}
                title={file.name}
                onReady={handlePresentationReady}
                onError={(_error, sourceKey) => handlePresentationError(sourceKey)}
              />
            )
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
  const supportsMarkdownEdit = descriptor.kind === 'markdown' && !effectiveReadOnly && !retainedSessionDraft;
  const supportsWrapToggle = viewMode === 'source' || descriptor.kind === 'code' || descriptor.kind === 'text';
  const presentationRendererStarting = descriptor.kind === 'presentation'
    && hasCurrentPresentationSource
    && previewLoadState === 'initial-loading';

  const previewCard = (
    <div className={cardClassName} onClick={inline ? undefined : (event) => event.stopPropagation()} data-testid={inline ? 'file-preview-inline' : undefined}>
      <div className={headerClassName}>
        <div className="flex min-w-0 flex-1 items-center gap-2">
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
          {backgroundRefreshing && loading && <span role="status" className="text-[11px] text-claude-muted">正在更新文件</span>}
          {descriptor.kind === 'spreadsheet' && spreadsheetSaveError && (
            <span role="alert" title={spreadsheetSaveError} className="max-w-[120px] truncate text-[11px] text-claude-error">保存失败</span>
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
          {onOpenWorkspace && (
            <button
              type="button"
              onClick={onOpenWorkspace}
              className="inline-flex h-8 items-center gap-1.5 rounded-md px-2 text-claude-accent transition-colors hover:bg-claude-accent/10 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-claude-accent/45"
              aria-label="在工作区打开"
              title="在工作区打开"
            >
              <Folder size={16} aria-hidden="true" />
              <span className="hidden whitespace-nowrap md:inline">在工作区打开</span>
            </button>
          )}
          {canSaveToWorkspace && (
            <button
              type="button"
              onClick={() => void openWorkspacePicker()}
              disabled={workspaceImportPreparing || previewSaving}
              className="inline-flex h-8 items-center gap-1.5 rounded-md px-2 text-claude-muted transition-colors hover:bg-claude-accent/10 hover:text-claude-accent focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-claude-accent/45 disabled:cursor-wait disabled:opacity-45"
              title="存入工作区"
              aria-label="将当前会话文件存入工作区"
            >
              {workspaceImportPreparing
                ? <span className="h-4 w-4 animate-spin rounded-full border-2 border-current border-r-transparent" aria-hidden="true" />
                : <Folder size={16} aria-hidden="true" />}
              <span className="hidden whitespace-nowrap md:inline">存入工作区</span>
            </button>
          )}
          <button type="button" onClick={handleDownload} className="rounded-md p-1.5 text-claude-muted transition-colors hover:bg-claude-hover hover:text-claude-text" title="下载文件" aria-label="下载文件"><Download size={16} aria-hidden="true" /></button>
          {!inline && <button type="button" ref={closeButtonRef} onClick={onClose} className={closeButtonClassName} title="关闭" aria-label="关闭"><X size={17} aria-hidden="true" /></button>}
        </div>
      </div>

      {contextNotice && (
        <div className="flex shrink-0 items-center gap-2 border-b border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-900" role="status">
          <AlertCircle size={14} className="shrink-0" aria-hidden="true" />
          <span className="min-w-0 flex-1">{contextNotice}</span>
        </div>
      )}

      {retainedSessionDraft && (
        <div
          className="flex shrink-0 flex-wrap items-center gap-2 border-b border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-950"
          role="alert"
          data-testid="retained-session-draft"
        >
          <AlertCircle size={14} className="shrink-0" aria-hidden="true" />
          <span className="min-w-[220px] flex-1">{retainedSessionDraft.errorMessage || '本地草稿基于旧版本，已保留且不会覆盖当前文件。'}</span>
          <button
            type="button"
            onClick={handleDownloadRetainedDraft}
            className="h-8 shrink-0 rounded-md border border-amber-300 bg-white px-2.5 font-medium hover:bg-amber-100 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-amber-500/40"
          >
            下载草稿
          </button>
          <button
            type="button"
            onClick={() => void handleDiscardRetainedDraft()}
            className="h-8 shrink-0 rounded-md px-2.5 font-medium text-amber-950 hover:bg-amber-100 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-amber-500/40"
          >
            丢弃草稿
          </button>
        </div>
      )}

      {workspaceDraftLossNotice && (
        <div
          className="flex shrink-0 items-center gap-2 border-b border-red-200 bg-red-50 px-3 py-2 text-xs text-red-900"
          role="alert"
          data-testid="workspace-draft-loss-notice"
        >
          <AlertCircle size={14} className="shrink-0" aria-hidden="true" />
          <span className="min-w-0 flex-1">
            {workspaceDraftLossNotice.message}
            {workspaceDraftLossNotice.lastSavedAt
              ? ` 最近保存：${formatModifiedTime(workspaceDraftLossNotice.lastSavedAt)}`
              : ''}
          </span>
          <button
            type="button"
            onClick={() => setWorkspaceDraftLossNotice(null)}
            className="inline-flex h-8 w-8 shrink-0 items-center justify-center rounded-md text-red-700 hover:bg-red-100 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-red-500/40"
            aria-label="关闭未保存提示"
          >
            <X size={15} aria-hidden="true" />
          </button>
        </div>
      )}

      {previewNotice && refreshingRequestIdRef.current !== null && (
        <div className="flex shrink-0 items-center gap-2 border-b border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-900" role="status">
          <AlertCircle size={14} className="shrink-0" aria-hidden="true" />
          <span className="min-w-0 flex-1">{previewNotice}</span>
          <button type="button" onClick={() => { setPreviewNotice(''); setLocalReloadNonce((value) => value + 1); }} className="h-8 shrink-0 rounded-md border border-amber-300 bg-white px-2.5 font-medium hover:bg-amber-100 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-amber-500/40">重试加载</button>
        </div>
      )}

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
          <div role="alert" className="flex items-center gap-3 rounded-2xl border border-red-100 bg-red-50 p-4"><AlertCircle className="h-5 w-5 text-claude-error" /><span className="font-medium text-claude-error">{error}</span></div>
        ) : loading && !backgroundRefreshing && !presentationRendererStarting ? (
          <div className="flex flex-col items-center justify-center gap-4 py-24 text-center" data-testid="file-preview-loading" role="status" aria-live="polite">
            <div className="flex space-x-2" aria-hidden="true">{[0, 1, 2].map((index) => <div key={index} className="h-2 w-2 animate-dot-pulse rounded-full bg-claude-accent" style={{ animationDelay: `${index * 200}ms` }} />)}</div>
            {(descriptor.kind === 'document' || descriptor.kind === 'presentation') && (
              <div className="max-w-sm text-sm text-claude-secondary">
                <p className="font-medium text-claude-text">正在生成 PDF 预览</p>
                {officePreviewSlow && <p className="mt-1 text-xs leading-5 text-claude-muted">首次转换可能需要几十秒，你可以继续等待或下载原文件。</p>}
              </div>
            )}
          </div>
        ) : renderPreviewBody()}
      </div>
    </div>
  );

  const workspaceOverlays = canSaveToWorkspace ? (
    <>
      <WorkspaceDestinationPicker
        open={workspacePickerOpen}
        sessionId={sessionId}
        sourceFile={fileVersionRef.current || file}
        onClose={() => setWorkspacePickerOpen(false)}
        onImported={(result) => {
          setWorkspacePickerOpen(false);
          setWorkspaceNotice({
            entryId: result.entry.entry_id,
            path: result.entry.path,
            message: result.status === 'NO_CHANGE'
              ? '工作区已存在相同文件'
              : `已存入 工作区/${result.entry.path}`,
          });
        }}
      />
      {workspaceNotice && (
        <div className="fixed bottom-5 left-1/2 z-[190] flex max-w-[calc(100vw-32px)] -translate-x-1/2 items-center gap-3 rounded-xl border border-claude-border bg-white px-4 py-3 text-sm text-claude-text shadow-xl" role="status">
          <span className="min-w-0 truncate">{workspaceNotice.message}</span>
          <button type="button" onClick={() => { requestOpenWorkspace(workspaceNotice.entryId); setWorkspaceNotice(null); }} className="shrink-0 rounded-md px-2 py-1 font-semibold text-claude-accent hover:bg-claude-accent/10 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-claude-accent/45">打开工作区</button>
          <button type="button" onClick={() => setWorkspaceNotice(null)} className="inline-flex h-8 w-8 shrink-0 items-center justify-center rounded-md text-claude-muted hover:bg-claude-hover" aria-label="关闭提示"><X size={15} aria-hidden="true" /></button>
        </div>
      )}
    </>
  ) : null;

  if (inline) return <>{previewCard}{workspaceOverlays}</>;

  return (
    <>
      <div className="fixed inset-0 z-[100] flex animate-fade-in items-center justify-center p-6 sm:p-12" onClick={onClose}>
        <div className="absolute inset-0 bg-black/40 backdrop-blur-md" />
        {previewCard}
      </div>
      {workspaceOverlays}
    </>
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
