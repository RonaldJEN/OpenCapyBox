import { useCallback, useEffect, useRef, useState, type KeyboardEvent } from 'react';
import {
  ArrowLeft,
  Check,
  ChevronDown,
  ChevronRight,
  Loader2,
  Search,
  X,
} from 'lucide-react';

import { workspaceApi, workspaceEntryToFileInfo, WorkspaceApiError } from '../../services/workspaceApi';
import type { WorkspaceEntry } from '../../types/workspace';
import { getFileIcon, getFileIconClass } from '../../utils/fileUtils';

const ROOT_KEY = '__workspace_file_picker_root__';

function pickerError(error: unknown): string {
  if (error instanceof WorkspaceApiError) return error.detail.message;
  return error instanceof Error ? error.message : '工作区文件加载失败';
}

function EntryGlyph({ entry }: { entry: WorkspaceEntry }) {
  const fileInfo = workspaceEntryToFileInfo(entry);
  const Glyph = getFileIcon(fileInfo);
  return <Glyph className={`h-4 w-4 shrink-0 ${getFileIconClass(fileInfo)}`} aria-hidden="true" />;
}

function PickerTreeEntry({
  entry,
  depth,
  expanded,
  loadingDirectories,
  childrenByParent,
  selected,
  onToggleDirectory,
  onToggleEntry,
}: {
  entry: WorkspaceEntry;
  depth: number;
  expanded: Set<string>;
  loadingDirectories: Set<string>;
  childrenByParent: Map<string, WorkspaceEntry[]>;
  selected: Map<string, WorkspaceEntry>;
  onToggleDirectory: (entry: WorkspaceEntry) => void;
  onToggleEntry: (entry: WorkspaceEntry) => void;
}) {
  const directory = entry.kind === 'directory';
  const opened = directory && expanded.has(entry.entry_id);
  const children = childrenByParent.get(entry.entry_id) || [];
  const handleKeyDown = (event: KeyboardEvent<HTMLButtonElement>) => {
    const current = event.currentTarget;
    const buttons = Array.from(current.closest('[role="tree"]')?.querySelectorAll<HTMLButtonElement>('[data-workspace-picker-tree-entry]') || []);
    const index = buttons.indexOf(current);
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault();
      buttons[event.key === 'ArrowDown' ? Math.min(buttons.length - 1, index + 1) : Math.max(0, index - 1)]?.focus();
    } else if (event.key === 'ArrowRight' && directory) {
      event.preventDefault();
      if (!opened) onToggleDirectory(entry);
      else current.closest('li[role="treeitem"]')?.querySelector<HTMLButtonElement>('ul[role="group"] [data-workspace-picker-tree-entry]')?.focus();
    } else if (event.key === 'ArrowLeft' && directory && opened) {
      event.preventDefault(); onToggleDirectory(entry);
    } else if (event.key === 'Enter') {
      event.preventDefault(); onToggleEntry(entry);
    }
  };
  return (
    <li role="treeitem" aria-expanded={directory ? opened : undefined} aria-selected={selected.has(entry.entry_id)}>
      <div className="flex min-h-11 items-center rounded-lg pr-2 hover:bg-claude-hover" style={{ paddingLeft: `${5 + depth * 16}px` }}>
        {directory ? (
          <button type="button" onClick={() => onToggleDirectory(entry)} className="inline-flex h-10 w-9 shrink-0 items-center justify-center rounded-md text-claude-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-claude-accent/40" aria-label={`${opened ? '收起' : '展开'} ${entry.name}`}>
            {loadingDirectories.has(entry.entry_id)
              ? <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" />
              : opened ? <ChevronDown className="h-4 w-4" /> : <ChevronRight className="h-4 w-4" />}
          </button>
        ) : <span className="w-9 shrink-0" />}
        <button type="button" title={entry.name} data-workspace-picker-tree-entry onKeyDown={handleKeyDown} onClick={() => onToggleEntry(entry)} aria-pressed={selected.has(entry.entry_id)} className="flex min-w-0 flex-1 items-center gap-2 self-stretch rounded-md text-left text-sm text-claude-text focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-claude-accent/40">
          <span className={`flex h-5 w-5 shrink-0 items-center justify-center rounded border ${selected.has(entry.entry_id) ? 'border-claude-accent bg-claude-accent text-white' : 'border-claude-border bg-white'}`}>{selected.has(entry.entry_id) && <Check className="h-3.5 w-3.5" />}</span>
          <EntryGlyph entry={entry} /><span className="min-w-0 flex-1 truncate">{entry.name}</span>
        </button>
      </div>
      {opened && children.length > 0 && (
        <ul role="group">
          {children.map((child) => <PickerTreeEntry key={child.entry_id} entry={child} depth={depth + 1} expanded={expanded} loadingDirectories={loadingDirectories} childrenByParent={childrenByParent} selected={selected} onToggleDirectory={onToggleDirectory} onToggleEntry={onToggleEntry} />)}
        </ul>
      )}
    </li>
  );
}

export function WorkspaceFilePicker({
  onBack,
  onClose,
  onConfirm,
}: {
  onBack: () => void;
  onClose: () => void;
  onConfirm: (entries: WorkspaceEntry[]) => void;
}) {
  const [childrenByParent, setChildrenByParent] = useState<Map<string, WorkspaceEntry[]>>(new Map());
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const [loadingDirectories, setLoadingDirectories] = useState<Set<string>>(new Set());
  const [selected, setSelected] = useState<Map<string, WorkspaceEntry>>(new Map());
  const [query, setQuery] = useState('');
  const [searchResults, setSearchResults] = useState<WorkspaceEntry[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const requestRef = useRef(0);

  const loadDirectory = useCallback(async (parentId: string | null) => {
    const key = parentId || ROOT_KEY;
    setLoadingDirectories((current) => new Set(current).add(key));
    try {
      const response = await workspaceApi.listAllEntries({ parentId });
      setChildrenByParent((current) => {
        const next = new Map(current);
        next.set(key, response.items.filter((entry) => entry.status === 'active'));
        return next;
      });
      setError('');
    } catch (loadError) {
      setError(pickerError(loadError));
    } finally {
      setLoadingDirectories((current) => {
        const next = new Set(current);
        next.delete(key);
        return next;
      });
      setLoading(false);
    }
  }, []);

  useEffect(() => { void loadDirectory(null); }, [loadDirectory]);

  useEffect(() => {
    if (!query.trim()) {
      setSearchResults([]);
      return undefined;
    }
    const request = ++requestRef.current;
    setLoading(true);
    const timer = window.setTimeout(() => {
      void workspaceApi.listAllEntries({ query: query.trim() }).then((response) => {
        if (request === requestRef.current) setSearchResults(response.items.filter((entry) => entry.status === 'active'));
      }).catch((searchError) => {
        if (request === requestRef.current) setError(pickerError(searchError));
      }).finally(() => {
        if (request === requestRef.current) setLoading(false);
      });
    }, 180);
    return () => window.clearTimeout(timer);
  }, [query]);

  const toggleDirectory = async (entry: WorkspaceEntry) => {
    if (expanded.has(entry.entry_id)) {
      setExpanded((current) => { const next = new Set(current); next.delete(entry.entry_id); return next; });
      return;
    }
    setExpanded((current) => new Set(current).add(entry.entry_id));
    if (!childrenByParent.has(entry.entry_id)) await loadDirectory(entry.entry_id);
  };

  const toggleEntry = (entry: WorkspaceEntry) => {
    setSelected((current) => {
      const next = new Map(current);
      if (next.has(entry.entry_id)) next.delete(entry.entry_id);
      else next.set(entry.entry_id, entry);
      return next;
    });
  };

  const rootEntries = childrenByParent.get(ROOT_KEY) || [];
  return (
    <div className="flex max-h-[70vh] min-h-[360px] flex-col" aria-label="选择工作区文件">
      <div className="border-b border-claude-border p-3">
        <div className="mb-2 flex items-center gap-2">
          <button type="button" onClick={onBack} className="rounded-lg p-1.5 text-claude-muted hover:bg-claude-hover focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-claude-accent/40" aria-label="返回添加内容"><ArrowLeft className="h-4 w-4" /></button>
          <div id="composer-workspace-picker-title" className="min-w-0 flex-1 text-sm font-medium text-claude-text">工作区文件</div>
          <button type="button" onClick={onClose} className="rounded-lg p-1.5 text-claude-muted hover:bg-claude-hover focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-claude-accent/40 md:hidden" aria-label="关闭工作区文件选择器"><X className="h-4 w-4" /></button>
        </div>
        <label className="flex h-10 items-center gap-2 rounded-xl border border-claude-border px-3 focus-within:border-claude-border-strong focus-within:ring-2 focus-within:ring-claude-accent/10">
          <Search className="h-4 w-4 text-claude-muted" /><span className="sr-only">搜索工作区文件</span>
          <input autoFocus value={query} onChange={(event) => setQuery(event.target.value)} placeholder="搜索名称或路径" className="min-w-0 flex-1 border-0 bg-transparent p-0 text-sm text-claude-text outline-none placeholder:text-claude-muted focus:ring-0" />
          {loading && <Loader2 className="h-4 w-4 animate-spin text-claude-muted" aria-label="正在加载工作区文件" />}
        </label>
      </div>
      <div className="min-h-0 flex-1 overflow-y-auto p-2">
        {error && <div className="m-2 rounded-lg bg-red-50 px-3 py-2 text-xs text-claude-error" role="alert">{error}</div>}
        {!loading && (query.trim() ? searchResults : rootEntries).length === 0 && <div className="p-8 text-center text-sm text-claude-muted">{query.trim() ? '没有匹配项目' : '工作区暂无文件或文件夹'}</div>}
        {query.trim() ? (
          <div role="group" aria-label="工作区文件搜索结果">
            {searchResults.map((entry) => <button key={entry.entry_id} type="button" onClick={() => toggleEntry(entry)} aria-pressed={selected.has(entry.entry_id)} className="flex min-h-11 w-full items-center gap-2 rounded-lg px-3 text-left text-sm hover:bg-claude-hover focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-claude-accent/40"><span className={`flex h-5 w-5 items-center justify-center rounded border ${selected.has(entry.entry_id) ? 'border-claude-accent bg-claude-accent text-white' : 'border-claude-border'}`}>{selected.has(entry.entry_id) && <Check className="h-3.5 w-3.5" />}</span><EntryGlyph entry={entry} /><span className="min-w-0 flex-1"><span className="block truncate">{entry.name}</span><span className="block truncate text-[10px] text-claude-muted">工作区/{entry.path}</span></span></button>)}
          </div>
        ) : (
          <ul role="tree" aria-label="工作区文件树">{rootEntries.map((entry) => <PickerTreeEntry key={entry.entry_id} entry={entry} depth={0} expanded={expanded} loadingDirectories={loadingDirectories} childrenByParent={childrenByParent} selected={selected} onToggleDirectory={(directory) => void toggleDirectory(directory)} onToggleEntry={toggleEntry} />)}</ul>
        )}
      </div>
      <div className="flex shrink-0 items-center justify-between gap-3 border-t border-claude-border p-3">
        <span className="text-xs text-claude-muted">已选择 {selected.size} 个项目</span>
        <button type="button" onClick={() => onConfirm(Array.from(selected.values()))} disabled={selected.size === 0} className="h-10 rounded-lg bg-claude-accent px-4 text-sm font-semibold text-white disabled:opacity-40">添加到对话</button>
      </div>
    </div>
  );
}
