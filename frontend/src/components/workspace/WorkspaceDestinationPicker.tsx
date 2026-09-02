import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type KeyboardEvent,
} from 'react';
import { createPortal } from 'react-dom';
import {
  Check,
  ChevronDown,
  ChevronRight,
  FileInput,
  Folder,
  FolderPlus,
  Loader2,
  Search,
  X,
} from 'lucide-react';

import type { FileInfo } from '../../types';
import type { WorkspaceEntry, WorkspaceMutationResult } from '../../types/workspace';
import {
  createWorkspaceIdempotencyKey,
  WorkspaceApiError,
  workspaceApi,
} from '../../services/workspaceApi';
import { emitWorkspaceMutation } from '../../services/workspaceEvents';

interface WorkspaceDestinationPickerProps {
  open: boolean;
  sessionId: string;
  sourceFile: FileInfo;
  onClose: () => void;
  onImported: (result: WorkspaceMutationResult) => void;
}

const ROOT_KEY = '__workspace_root__';

function parentKey(parentId: string | null): string {
  return parentId || ROOT_KEY;
}

function workspaceErrorMessage(error: unknown): string {
  if (error instanceof WorkspaceApiError) {
    if (error.detail.code === 'SOURCE_REVISION_CONFLICT') return '会话文件已被更新，请刷新文件后重试。';
    if (error.detail.code === 'REVISION_CONFLICT') return '目标文件已被其他任务更新，请重新选择后再试。';
    if (error.detail.code === 'QUOTA_EXCEEDED') return '工作区容量不足，请清理文件后重试。';
    return error.detail.message;
  }
  return error instanceof Error ? error.message : '存入工作区失败，请重试。';
}

function DirectoryRow({
  entry,
  depth,
  selectedId,
  expanded,
  childrenByParent,
  loadingParents,
  onSelect,
  onToggle,
}: {
  entry: WorkspaceEntry;
  depth: number;
  selectedId: string | null;
  expanded: Set<string>;
  childrenByParent: Map<string, WorkspaceEntry[]>;
  loadingParents: Set<string>;
  onSelect: (entry: WorkspaceEntry) => void;
  onToggle: (entry: WorkspaceEntry) => void;
}) {
  const isExpanded = expanded.has(entry.entry_id);
  const children = childrenByParent.get(entry.entry_id) || [];
  const loading = loadingParents.has(entry.entry_id);
  const handleTreeKeyDown = (event: KeyboardEvent<HTMLButtonElement>) => {
    const current = event.currentTarget;
    const tree = current.closest('[role="tree"]');
    const visibleEntries = Array.from(tree?.querySelectorAll<HTMLButtonElement>('[data-workspace-directory-entry]') || []);
    const index = visibleEntries.indexOf(current);
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault();
      const nextIndex = event.key === 'ArrowDown'
        ? Math.min(visibleEntries.length - 1, index + 1)
        : Math.max(0, index - 1);
      visibleEntries[nextIndex]?.focus();
      return;
    }
    if (event.key === 'Home' || event.key === 'End') {
      event.preventDefault();
      (event.key === 'Home' ? visibleEntries[0] : visibleEntries[visibleEntries.length - 1])?.focus();
      return;
    }
    if (event.key === 'ArrowRight') {
      event.preventDefault();
      if (!isExpanded) onToggle(entry);
      else current.closest('li[role="treeitem"]')?.querySelector<HTMLButtonElement>('ul[role="group"] [data-workspace-directory-entry]')?.focus();
      return;
    }
    if (event.key === 'ArrowLeft') {
      event.preventDefault();
      if (isExpanded) onToggle(entry);
      else current.closest('li[role="treeitem"]')?.parentElement?.closest('li[role="treeitem"]')?.querySelector<HTMLButtonElement>(':scope > div [data-workspace-directory-entry]')?.focus();
      return;
    }
    if (event.key === 'Enter') {
      event.preventDefault();
      onSelect(entry);
    }
  };
  return (
    <li role="treeitem" aria-expanded={isExpanded} aria-selected={selectedId === entry.entry_id}>
      <div
        className={`group flex min-h-11 items-center rounded-lg pr-2 transition-colors ${
          selectedId === entry.entry_id
            ? 'bg-claude-accent/10 text-claude-text'
            : 'text-claude-secondary hover:bg-claude-hover hover:text-claude-text'
        }`}
        style={{ paddingLeft: `${8 + depth * 18}px` }}
      >
        <button
          type="button"
          onClick={() => onToggle(entry)}
          aria-label={`${isExpanded ? '收起' : '展开'} ${entry.name}`}
          className="inline-flex h-9 w-9 shrink-0 items-center justify-center rounded-md text-claude-muted hover:bg-white/70 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-claude-accent/45"
        >
          {loading
            ? <Loader2 size={15} className="animate-spin" aria-hidden="true" />
            : isExpanded
              ? <ChevronDown size={15} aria-hidden="true" />
              : <ChevronRight size={15} aria-hidden="true" />}
        </button>
        <button
          type="button"
          data-workspace-directory-entry
          onClick={() => onSelect(entry)}
          onKeyDown={handleTreeKeyDown}
          className="flex min-w-0 flex-1 items-center gap-2 self-stretch rounded-md text-left text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-claude-accent/45"
        >
          <Folder size={17} className="shrink-0 text-claude-accent" aria-hidden="true" />
          <span className="truncate">{entry.name}</span>
          {selectedId === entry.entry_id && <Check size={15} className="ml-auto shrink-0 text-claude-accent" aria-hidden="true" />}
        </button>
      </div>
      {isExpanded && children.length > 0 && (
        <ul role="group">
          {children.map((child) => (
            <DirectoryRow
              key={child.entry_id}
              entry={child}
              depth={depth + 1}
              selectedId={selectedId}
              expanded={expanded}
              childrenByParent={childrenByParent}
              loadingParents={loadingParents}
              onSelect={onSelect}
              onToggle={onToggle}
            />
          ))}
        </ul>
      )}
    </li>
  );
}

export function WorkspaceDestinationPicker({
  open,
  sessionId,
  sourceFile,
  onClose,
  onImported,
}: WorkspaceDestinationPickerProps) {
  const [childrenByParent, setChildrenByParent] = useState<Map<string, WorkspaceEntry[]>>(new Map());
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const [loadingParents, setLoadingParents] = useState<Set<string>>(new Set());
  const [selectedDirectory, setSelectedDirectory] = useState<WorkspaceEntry | null>(null);
  const [query, setQuery] = useState('');
  const [searchResults, setSearchResults] = useState<WorkspaceEntry[]>([]);
  const [searching, setSearching] = useState(false);
  const [fileName, setFileName] = useState(sourceFile.name);
  const [newFolderOpen, setNewFolderOpen] = useState(false);
  const [newFolderName, setNewFolderName] = useState('');
  const [creatingFolder, setCreatingFolder] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState('');
  const [conflictEntry, setConflictEntry] = useState<WorkspaceEntry | null>(null);
  const dialogRef = useRef<HTMLDivElement>(null);
  const searchInputRef = useRef<HTMLInputElement>(null);
  const previousFocusRef = useRef<HTMLElement | null>(null);
  const requestSequenceRef = useRef(0);
  const idempotencyKeyRef = useRef(createWorkspaceIdempotencyKey('import-session-file'));

  const loadDirectories = useCallback(async (parentId: string | null, force = false) => {
    const key = parentKey(parentId);
    if (!force && childrenByParent.has(key)) return;
    setLoadingParents((current) => new Set(current).add(key));
    try {
      const response = await workspaceApi.listAllEntries({ parentId });
      const directories = response.items.filter((entry) => entry.kind === 'directory' && entry.status === 'active');
      setChildrenByParent((current) => {
        const next = new Map(current);
        next.set(key, directories);
        return next;
      });
    } catch (loadError) {
      setError(workspaceErrorMessage(loadError));
    } finally {
      setLoadingParents((current) => {
        const next = new Set(current);
        next.delete(key);
        return next;
      });
    }
  }, [childrenByParent]);

  useEffect(() => {
    if (!open) return;
    setChildrenByParent(new Map());
    setExpanded(new Set());
    setSelectedDirectory(null);
    setQuery('');
    setSearchResults([]);
    setFileName(sourceFile.name);
    setNewFolderOpen(false);
    setNewFolderName('');
    setConflictEntry(null);
    setError('');
    idempotencyKeyRef.current = createWorkspaceIdempotencyKey('import-session-file');
    void workspaceApi.listAllEntries().then((response) => {
      setChildrenByParent(new Map([[
        ROOT_KEY,
        response.items.filter((entry) => entry.kind === 'directory' && entry.status === 'active'),
      ]]));
    }).catch((loadError) => setError(workspaceErrorMessage(loadError)));
  }, [open, sourceFile.name]);

  useEffect(() => {
    if (!open) return undefined;
    previousFocusRef.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    window.requestAnimationFrame(() => searchInputRef.current?.focus({ preventScroll: true }));
    return () => {
      document.body.style.overflow = previousOverflow;
      previousFocusRef.current?.focus({ preventScroll: true });
    };
  }, [open]);

  useEffect(() => {
    if (!open || !query.trim()) {
      setSearchResults([]);
      setSearching(false);
      return undefined;
    }
    const sequence = ++requestSequenceRef.current;
    setSearching(true);
    const timer = window.setTimeout(() => {
      void workspaceApi.listAllEntries({ query: query.trim() }).then((response) => {
        if (sequence !== requestSequenceRef.current) return;
        setSearchResults(response.items.filter((entry) => entry.kind === 'directory' && entry.status === 'active'));
      }).catch((searchError) => {
        if (sequence === requestSequenceRef.current) setError(workspaceErrorMessage(searchError));
      }).finally(() => {
        if (sequence === requestSequenceRef.current) setSearching(false);
      });
    }, 180);
    return () => window.clearTimeout(timer);
  }, [open, query]);

  const toggleDirectory = async (entry: WorkspaceEntry) => {
    if (expanded.has(entry.entry_id)) {
      setExpanded((current) => {
        const next = new Set(current);
        next.delete(entry.entry_id);
        return next;
      });
      return;
    }
    setExpanded((current) => new Set(current).add(entry.entry_id));
    await loadDirectories(entry.entry_id);
  };

  const selectDirectory = (entry: WorkspaceEntry | null) => {
    setSelectedDirectory(entry);
    setConflictEntry(null);
    setError('');
    idempotencyKeyRef.current = createWorkspaceIdempotencyKey('import-session-file');
  };

  const createFolder = async () => {
    const normalizedName = newFolderName.trim();
    if (!normalizedName || creatingFolder) return;
    setCreatingFolder(true);
    setError('');
    try {
      const result = await workspaceApi.createDirectory(selectedDirectory?.entry_id || null, normalizedName);
      emitWorkspaceMutation({ operation: 'create_directory', entry: result.entry, parentId: result.entry.parent_id });
      const parentId = selectedDirectory?.entry_id || null;
      await loadDirectories(parentId, true);
      setSelectedDirectory(result.entry);
      setExpanded((current) => new Set(current).add(result.entry.entry_id));
      setNewFolderName('');
      setNewFolderOpen(false);
    } catch (createError) {
      setError(workspaceErrorMessage(createError));
    } finally {
      setCreatingFolder(false);
    }
  };

  const submitImport = async (overwrite = false) => {
    const sourceRevision = sourceFile.revision == null ? '' : String(sourceFile.revision);
    if (!sourceRevision) {
      setError('文件版本尚未就绪，请刷新会话文件后重试。');
      return;
    }
    const normalizedName = fileName.trim();
    if (!normalizedName || submitting) return;
    setSubmitting(true);
    setError('');
    try {
      const result = await workspaceApi.importSessionFile({
        session_id: sessionId,
        source_path: sourceFile.path,
        source_revision: sourceRevision,
        destination_parent_id: selectedDirectory?.entry_id || null,
        destination_name: normalizedName,
        conflict_policy: overwrite ? 'overwrite' : 'fail',
        expected_destination_revision: overwrite ? conflictEntry?.revision ?? null : null,
        idempotency_key: idempotencyKeyRef.current,
      });
      emitWorkspaceMutation({ operation: 'import_session_file', entry: result.entry, parentId: result.entry.parent_id });
      onImported(result);
    } catch (importError) {
      if (importError instanceof WorkspaceApiError && importError.detail.code === 'NAME_CONFLICT') {
        setConflictEntry(importError.detail.entry || null);
        setError(importError.detail.entry ? '' : importError.detail.message);
      } else if (importError instanceof WorkspaceApiError && importError.detail.code === 'REVISION_CONFLICT' && importError.detail.entry) {
        setConflictEntry(importError.detail.entry);
        setError('目标文件刚刚发生变化，请确认最新版本后再次覆盖。');
      } else {
        setError(workspaceErrorMessage(importError));
      }
    } finally {
      setSubmitting(false);
    }
  };

  const handleDialogKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    if (event.key === 'Escape') {
      event.stopPropagation();
      if (conflictEntry) setConflictEntry(null);
      else onClose();
      return;
    }
    if (event.key !== 'Tab') return;
    const focusable = Array.from(dialogRef.current?.querySelectorAll<HTMLElement>(
      'button:not([disabled]), input:not([disabled]), [tabindex]:not([tabindex="-1"])',
    ) || []).filter((element) => element.offsetParent !== null);
    if (focusable.length === 0) return;
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  };

  const visibleDirectories = useMemo(
    () => query.trim() ? searchResults : (childrenByParent.get(ROOT_KEY) || []),
    [childrenByParent, query, searchResults],
  );

  if (!open) return null;

  return createPortal(
    <div
      className="fixed inset-0 z-[180] flex items-center justify-center bg-black/35 p-0 backdrop-blur-[2px] sm:p-6"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget && !submitting) onClose();
      }}
    >
      <div
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby="workspace-picker-title"
        tabIndex={-1}
        onKeyDown={handleDialogKeyDown}
        className="flex h-[100dvh] w-full flex-col overflow-hidden bg-white outline-none sm:h-[min(720px,88vh)] sm:max-w-[720px] sm:rounded-2xl sm:border sm:border-claude-border sm:shadow-2xl"
      >
        <header className="flex min-h-16 shrink-0 items-center justify-between border-b border-claude-border px-5 sm:px-6">
          <div className="min-w-0">
            <h2 id="workspace-picker-title" className="text-lg font-semibold text-claude-text">存入工作区</h2>
            <p className="mt-0.5 truncate text-xs text-claude-muted" title={sourceFile.name}>{sourceFile.name}</p>
          </div>
          <button type="button" onClick={onClose} disabled={submitting} className="inline-flex h-11 w-11 items-center justify-center rounded-lg text-claude-muted hover:bg-claude-hover focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-claude-accent/45 disabled:opacity-50" aria-label="关闭存入工作区窗口">
            <X size={20} aria-hidden="true" />
          </button>
        </header>

        {conflictEntry ? (
          <div className="flex min-h-0 flex-1 flex-col px-5 py-6 sm:px-8">
            <div className="mx-auto flex w-full max-w-lg flex-1 flex-col justify-center">
              <div className="mb-5 flex h-12 w-12 items-center justify-center rounded-xl bg-amber-50 text-amber-700"><FileInput size={22} aria-hidden="true" /></div>
              <h3 className="text-lg font-semibold text-claude-text">目标目录已有同名文件</h3>
              <p className="mt-2 text-sm leading-6 text-claude-secondary">可修改文件名、返回选择其他目录，或覆盖当前版本。覆盖前会再次校验版本。</p>
              <label className="mt-5 text-sm font-medium text-claude-text">
                文件名
                <input
                  value={fileName}
                  onChange={(event) => {
                    setFileName(event.target.value);
                    idempotencyKeyRef.current = createWorkspaceIdempotencyKey('import-session-file');
                  }}
                  className="mt-2 h-11 w-full rounded-lg border border-claude-border px-3 text-sm outline-none focus:border-claude-accent focus:ring-2 focus:ring-claude-accent/20"
                />
              </label>
              {error && <p role="alert" className="mt-3 text-sm text-claude-error">{error}</p>}
            </div>
            <div className="flex flex-wrap justify-end gap-2 border-t border-claude-border pt-4">
              <button type="button" onClick={() => setConflictEntry(null)} disabled={submitting} className="h-11 rounded-lg border border-claude-border px-4 text-sm font-medium text-claude-text hover:bg-claude-hover disabled:opacity-50">选择其他目录</button>
              <button type="button" onClick={() => { setConflictEntry(null); void submitImport(false); }} disabled={!fileName.trim() || fileName.trim() === conflictEntry.name || submitting} className="h-11 rounded-lg border border-claude-border px-4 text-sm font-medium text-claude-text hover:bg-claude-hover disabled:opacity-40">改名保存</button>
              <button type="button" onClick={() => void submitImport(true)} disabled={submitting} className="inline-flex h-11 items-center gap-2 rounded-lg bg-claude-accent px-4 text-sm font-semibold text-white hover:opacity-90 disabled:opacity-50">
                {submitting && <Loader2 size={15} className="animate-spin" aria-hidden="true" />}覆盖文件
              </button>
            </div>
          </div>
        ) : (
          <>
            <div className="shrink-0 px-5 pb-3 pt-4 sm:px-6">
              <label className="flex h-11 items-center gap-2 rounded-lg border border-claude-border bg-claude-surface px-3 focus-within:border-claude-accent focus-within:ring-2 focus-within:ring-claude-accent/20">
                <Search size={17} className="shrink-0 text-claude-muted" aria-hidden="true" />
                <span className="sr-only">搜索工作区文件夹</span>
                <input ref={searchInputRef} value={query} onChange={(event) => setQuery(event.target.value)} placeholder="搜索文件夹名称或路径" className="min-w-0 flex-1 bg-transparent text-sm text-claude-text outline-none placeholder:text-claude-muted" />
                {searching && <Loader2 size={15} className="animate-spin text-claude-muted" aria-label="正在搜索" />}
              </label>
            </div>

            <div className="min-h-0 flex-1 overflow-y-auto px-4 pb-4 sm:px-5">
              <div
                className={`flex min-h-11 items-center rounded-lg px-2 transition-colors ${selectedDirectory === null ? 'bg-claude-accent/10 text-claude-text' : 'text-claude-secondary hover:bg-claude-hover'}`}
              >
                <span className="inline-flex h-9 w-9 shrink-0 items-center justify-center"><Folder size={18} className="text-claude-accent" aria-hidden="true" /></span>
                <button type="button" onClick={() => selectDirectory(null)} className="flex min-w-0 flex-1 items-center self-stretch rounded-md text-left text-sm font-medium focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-claude-accent/45">
                  工作区
                  {selectedDirectory === null && <Check size={15} className="ml-auto text-claude-accent" aria-hidden="true" />}
                </button>
              </div>
              {visibleDirectories.length === 0 && !searching ? (
                <div className="flex min-h-48 flex-col items-center justify-center text-center text-sm text-claude-muted">
                  <Folder size={34} className="mb-3 text-claude-border" aria-hidden="true" />
                  {query.trim() ? '没有匹配的文件夹' : '工作区还没有文件夹'}
                </div>
              ) : query.trim() ? (
                <ul className="mt-1 space-y-1" aria-label="文件夹搜索结果">
                  {visibleDirectories.map((entry) => (
                    <li key={entry.entry_id}>
                      <button type="button" onClick={() => selectDirectory(entry)} className={`flex min-h-11 w-full items-center gap-2 rounded-lg px-3 text-left text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-claude-accent/45 ${selectedDirectory?.entry_id === entry.entry_id ? 'bg-claude-accent/10 text-claude-text' : 'text-claude-secondary hover:bg-claude-hover'}`}>
                        <Folder size={17} className="shrink-0 text-claude-accent" aria-hidden="true" />
                        <span className="min-w-0 flex-1"><span className="block truncate font-medium">{entry.name}</span><span className="block truncate text-[11px] text-claude-muted">工作区/{entry.path}</span></span>
                        {selectedDirectory?.entry_id === entry.entry_id && <Check size={15} className="shrink-0 text-claude-accent" aria-hidden="true" />}
                      </button>
                    </li>
                  ))}
                </ul>
              ) : (
                <ul role="tree" aria-label="工作区文件夹" className="mt-1 space-y-1">
                  {visibleDirectories.map((entry) => (
                    <DirectoryRow key={entry.entry_id} entry={entry} depth={0} selectedId={selectedDirectory?.entry_id || null} expanded={expanded} childrenByParent={childrenByParent} loadingParents={loadingParents} onSelect={selectDirectory} onToggle={(directory) => void toggleDirectory(directory)} />
                  ))}
                </ul>
              )}
            </div>

            <div className="shrink-0 border-t border-claude-border bg-claude-surface/45 px-5 py-3 sm:px-6">
              {newFolderOpen ? (
                <div className="mb-3 flex items-center gap-2">
                  <input autoFocus value={newFolderName} onChange={(event) => setNewFolderName(event.target.value)} onKeyDown={(event) => { if (event.key === 'Enter') void createFolder(); }} placeholder="新文件夹名称" className="h-10 min-w-0 flex-1 rounded-lg border border-claude-border bg-white px-3 text-sm outline-none focus:border-claude-accent focus:ring-2 focus:ring-claude-accent/20" />
                  <button type="button" onClick={() => void createFolder()} disabled={!newFolderName.trim() || creatingFolder} className="inline-flex h-10 items-center gap-1.5 rounded-lg bg-claude-text px-3 text-sm font-medium text-white disabled:opacity-40">{creatingFolder && <Loader2 size={14} className="animate-spin" />}创建</button>
                  <button type="button" onClick={() => setNewFolderOpen(false)} disabled={creatingFolder} className="h-10 rounded-lg px-3 text-sm text-claude-secondary hover:bg-claude-hover">取消</button>
                </div>
              ) : (
                <button type="button" onClick={() => setNewFolderOpen(true)} className="mb-3 inline-flex h-10 items-center gap-2 rounded-lg px-2 text-sm font-medium text-claude-secondary hover:bg-claude-hover focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-claude-accent/45"><FolderPlus size={17} aria-hidden="true" />在当前目录新建文件夹</button>
              )}
              <label className="block text-xs font-medium text-claude-muted">
                保存为
                <input value={fileName} onChange={(event) => { setFileName(event.target.value); idempotencyKeyRef.current = createWorkspaceIdempotencyKey('import-session-file'); }} className="mt-1.5 h-10 w-full rounded-lg border border-claude-border bg-white px-3 text-sm text-claude-text outline-none focus:border-claude-accent focus:ring-2 focus:ring-claude-accent/20" />
              </label>
              {error && <p role="alert" className="mt-2 text-sm text-claude-error">{error}</p>}
              <div className="mt-3 flex items-center justify-between gap-3">
                <p className="min-w-0 truncate text-xs text-claude-muted" title={selectedDirectory ? `工作区/${selectedDirectory.path}` : '工作区'}>目标：{selectedDirectory ? `工作区/${selectedDirectory.path}` : '工作区'}</p>
                <div className="flex shrink-0 gap-2">
                  <button type="button" onClick={onClose} disabled={submitting} className="h-11 rounded-lg border border-claude-border bg-white px-5 text-sm font-medium text-claude-text hover:bg-claude-hover disabled:opacity-50">取消</button>
                  <button type="button" onClick={() => void submitImport(false)} disabled={!fileName.trim() || submitting} className="inline-flex h-11 min-w-[92px] items-center justify-center gap-2 rounded-lg bg-claude-accent px-5 text-sm font-semibold text-white hover:opacity-90 disabled:opacity-45">{submitting && <Loader2 size={15} className="animate-spin" aria-hidden="true" />}确定</button>
                </div>
              </div>
            </div>
          </>
        )}
      </div>
    </div>,
    document.body,
  );
}
