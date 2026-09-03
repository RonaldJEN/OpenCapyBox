import { useCallback, useEffect, useLayoutEffect, useRef, useState, type KeyboardEvent, type PointerEvent as ReactPointerEvent } from 'react';
import { createPortal } from 'react-dom';
import {
  ChevronDown,
  ChevronRight,
  FilePlus2,
  FileSpreadsheet,
  Folder,
  FolderPlus,
  Loader2,
  MoreHorizontal,
  Pencil,
  RefreshCw,
  Search,
  Trash2,
  Upload,
  X,
} from 'lucide-react';

import type { WorkspaceEntry } from '../../types/workspace';
import { emitWorkspaceMutation, subscribeWorkspaceMutation } from '../../services/workspaceEvents';
import { createWorkspaceIdempotencyKey, workspaceApi, workspaceEntryToFileInfo, WorkspaceApiError } from '../../services/workspaceApi';
import { getFileIcon, getFileIconClass } from '../../utils/fileUtils';
import { ConfirmDialog } from '../ConfirmDialog';
import { discardWorkspaceDrafts } from '../../services/workspaceDraftOutbox';

const ROOT = '__workspace_sidebar_root__';
const MAX_DIRECTORY_DEPTH = 2;

function errorText(error: unknown): string {
  if (error instanceof WorkspaceApiError) return error.detail.message;
  return error instanceof Error ? error.message : '工作区操作失败';
}

function revisionConflictEntry(error: unknown): WorkspaceEntry | null {
  return error instanceof WorkspaceApiError
    && error.detail.code === 'REVISION_CONFLICT'
    && error.detail.entry
    ? error.detail.entry
    : null;
}

function directoryDepth(entry: WorkspaceEntry): number {
  return entry.path.split('/').filter(Boolean).length;
}

function canCreateSubdirectory(entry: WorkspaceEntry): boolean {
  return entry.kind === 'directory' && directoryDepth(entry) < MAX_DIRECTORY_DEPTH;
}

function sortEntries(entries: WorkspaceEntry[]): WorkspaceEntry[] {
  return [...entries].sort((left, right) => (
    left.kind === right.kind
      ? left.name.localeCompare(right.name, 'zh-CN')
      : left.kind === 'directory' ? -1 : 1
  ));
}

function splitFileName(name: string): { stem: string; extension: string } {
  const extensionStart = name.lastIndexOf('.');
  if (extensionStart <= 0 || extensionStart === name.length - 1) {
    return { stem: name, extension: '' };
  }
  return {
    stem: name.slice(0, extensionStart),
    extension: name.slice(extensionStart),
  };
}

function preserveFileExtension(name: string, extension: string): string {
  const trimmed = name.trim();
  if (!extension) return trimmed;
  const stem = trimmed.toLocaleLowerCase().endsWith(extension.toLocaleLowerCase())
    ? trimmed.slice(0, -extension.length).trimEnd()
    : trimmed;
  return stem ? `${stem}${extension}` : '';
}

function isDescendantPath(path: string, directoryPath: string): boolean {
  return path.startsWith(`${directoryPath}/`);
}

function flattenVisibleEntries(
  children: Map<string, WorkspaceEntry[]>,
  expanded: Set<string>,
  parentId: string = ROOT,
): WorkspaceEntry[] {
  return (children.get(parentId) || []).flatMap((entry) => [
    entry,
    ...(entry.kind === 'directory' && expanded.has(entry.entry_id)
      ? flattenVisibleEntries(children, expanded, entry.entry_id)
      : []),
  ]);
}

function canonicalizeSelectedEntries(entries: WorkspaceEntry[]): WorkspaceEntry[] {
  const roots: WorkspaceEntry[] = [];
  [...entries]
    .sort((left, right) => directoryDepth(left) - directoryDepth(right))
    .forEach((entry) => {
      if (roots.some((root) => root.kind === 'directory' && isDescendantPath(entry.path, root.path))) return;
      roots.push(entry);
    });
  return roots;
}

function entryIntentSignature(entries: WorkspaceEntry[]): string {
  return [...new Set(entries.map((entry) => entry.entry_id))].sort().join('\0');
}

type NameAction = 'directory' | 'rename';
type DirectFileType = 'markdown' | 'xlsx';

export function WorkspaceSidebarContent({
  activeEntryId,
  isActive = true,
  onOpenEntry,
}: {
  activeEntryId?: string | null;
  isActive?: boolean;
  onOpenEntry: (entry: WorkspaceEntry) => void;
}) {
  const [children, setChildren] = useState<Map<string, WorkspaceEntry[]>>(new Map());
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const [loadingParents, setLoadingParents] = useState<Set<string>>(new Set());
  const [query, setQuery] = useState('');
  const [results, setResults] = useState<WorkspaceEntry[]>([]);
  const [resultsQuery, setResultsQuery] = useState('');
  const [searching, setSearching] = useState(false);
  const [searchRefreshToken, setSearchRefreshToken] = useState(0);
  const [menuOpen, setMenuOpen] = useState(false);
  const [menuPosition, setMenuPosition] = useState<{ left: number; top: number } | null>(null);
  const [searchOpen, setSearchOpen] = useState(false);
  const [rowMenuEntry, setRowMenuEntry] = useState<WorkspaceEntry | null>(null);
  const [rowMenuPosition, setRowMenuPosition] = useState<{ left: number; top: number } | null>(null);
  const [draggingEntry, setDraggingEntry] = useState<WorkspaceEntry | null>(null);
  const [dropTargetId, setDropTargetId] = useState<string | null>(null);
  const [dragPreviewPoint, setDragPreviewPoint] = useState<{ x: number; y: number } | null>(null);
  const [nameAction, setNameAction] = useState<NameAction | null>(null);
  const [nameDraft, setNameDraft] = useState('');
  const [createParent, setCreateParent] = useState<WorkspaceEntry | null>(null);
  const [actionEntry, setActionEntry] = useState<WorkspaceEntry | null>(null);
  const [deleteEntry, setDeleteEntry] = useState<WorkspaceEntry | null>(null);
  const [selectedEntryIds, setSelectedEntryIds] = useState<Set<string>>(new Set());
  const [selectionAnchorId, setSelectionAnchorId] = useState<string | null>(null);
  const [batchDeleteConfirmOpen, setBatchDeleteConfirmOpen] = useState(false);
  const [batchPendingIds, setBatchPendingIds] = useState<Set<string>>(new Set());
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const uploadRef = useRef<HTMLInputElement>(null);
  const uploadParentRef = useRef<WorkspaceEntry | null>(null);
  const menuButtonRef = useRef<HTMLButtonElement>(null);
  const menuPopupRef = useRef<HTMLDivElement>(null);
  const searchButtonRef = useRef<HTMLButtonElement>(null);
  const searchPopupRef = useRef<HTMLDivElement>(null);
  const rowMenuButtonRef = useRef<HTMLButtonElement | null>(null);
  const rowMenuPopupRef = useRef<HTMLDivElement>(null);
  const draggingEntryRef = useRef<WorkspaceEntry | null>(null);
  const pointerDragRef = useRef<{
    entry: WorkspaceEntry;
    pointerId: number;
    startX: number;
    startY: number;
    active: boolean;
  } | null>(null);
  const pointerDropTargetIdRef = useRef<string | null>(null);
  const dragPreviewPointRef = useRef({ x: 0, y: 0 });
  const dragPreviewFrameRef = useRef<number | null>(null);
  const suppressClickEntryIdRef = useRef<string | null>(null);
  const pointerListenersRef = useRef<{
    move: (event: globalThis.PointerEvent) => void;
    up: (event: globalThis.PointerEvent) => void;
    cancel: (event: globalThis.PointerEvent) => void;
  } | null>(null);
  const requestRef = useRef(0);
  const directoryRequestIdsRef = useRef(new Map<string, number>());
  const directCreateInFlightRef = useRef(false);
  const initialRootLoadStartedRef = useRef(false);
  const deleteIntentRef = useRef<{ signature: string; key: string } | null>(null);
  const authoritativeEntriesRef = useRef(new Map<string, WorkspaceEntry>());
  const directoryScopeRevisionsRef = useRef(new Map<string, Map<string, number>>());
  const searchScopeRevisionsRef = useRef<{ query: string; revisions: Map<string, number> } | null>(null);
  const wasActiveRef = useRef(isActive);

  const rememberAuthoritativeEntry = useCallback((entry: WorkspaceEntry) => {
    const current = authoritativeEntriesRef.current.get(entry.entry_id);
    if (current && current.revision > entry.revision) return;
    authoritativeEntriesRef.current.set(entry.entry_id, entry);
    directoryScopeRevisionsRef.current.forEach((revisions) => revisions.delete(entry.entry_id));
    if (entry.status === 'active') {
      directoryScopeRevisionsRef.current
        .get(entry.parent_id || ROOT)
        ?.set(entry.entry_id, entry.revision);
    }
    const searchScope = searchScopeRevisionsRef.current;
    searchScope?.revisions.delete(entry.entry_id);
    if (
      searchScope
      && entry.status === 'active'
      && entry.path.toLocaleLowerCase().includes(searchScope.query.toLocaleLowerCase())
    ) {
      searchScope.revisions.set(entry.entry_id, entry.revision);
    }
  }, []);

  const rememberAuthoritativeEntries = useCallback((entries: WorkspaceEntry[]) => {
    entries.forEach(rememberAuthoritativeEntry);
  }, [rememberAuthoritativeEntry]);

  const forgetAuthoritativeEntries = useCallback((entryIds: Iterable<string>) => {
    const ids = new Set(entryIds);
    for (const entryId of ids) authoritativeEntriesRef.current.delete(entryId);
    directoryScopeRevisionsRef.current.forEach((revisions) => {
      ids.forEach((entryId) => revisions.delete(entryId));
    });
    ids.forEach((entryId) => searchScopeRevisionsRef.current?.revisions.delete(entryId));
  }, []);

  const reconcileAuthoritativeScope = useCallback((
    previous: Map<string, number> | undefined,
    entries: WorkspaceEntry[],
  ) => {
    const next = new Map(entries.map((entry) => [entry.entry_id, entry.revision]));
    previous?.forEach((previousRevision, entryId) => {
      if (next.has(entryId)) return;
      const current = authoritativeEntriesRef.current.get(entryId);
      if (current && current.revision <= previousRevision) {
        authoritativeEntriesRef.current.delete(entryId);
      }
    });
    return next;
  }, []);

  const positionMenuBesideSidebar = useCallback(() => {
    const trigger = menuButtonRef.current;
    if (!trigger) return;
    const rect = trigger.getBoundingClientRect();
    const menuWidth = 172;
    const menuHeight = 188;
    const viewportGap = 8;
    const rightSide = rect.right + viewportGap;
    const leftSide = rect.left - viewportGap - menuWidth;
    setMenuPosition({
      left: rightSide + menuWidth <= window.innerWidth - viewportGap
        ? rightSide
        : Math.max(viewportGap, leftSide),
      top: Math.max(viewportGap, Math.min(rect.top, window.innerHeight - menuHeight - viewportGap)),
    });
  }, []);

  const openRowMenu = useCallback((entry: WorkspaceEntry, trigger: HTMLButtonElement) => {
    if (rowMenuEntry?.entry_id === entry.entry_id) {
      setRowMenuEntry(null);
      setRowMenuPosition(null);
      return;
    }
    const rect = trigger.getBoundingClientRect();
    const menuWidth = entry.kind === 'directory' ? 172 : 140;
    const menuHeight = entry.kind === 'directory'
      ? canCreateSubdirectory(entry) ? 276 : 240
      : 80;
    const viewportGap = 8;
    const rightSide = rect.right + viewportGap;
    const leftSide = rect.left - viewportGap - menuWidth;
    rowMenuButtonRef.current = trigger;
    setRowMenuEntry(entry);
    setRowMenuPosition({
      left: rightSide + menuWidth <= window.innerWidth - viewportGap
        ? rightSide
        : Math.max(viewportGap, leftSide),
      top: Math.max(viewportGap, Math.min(rect.top, window.innerHeight - menuHeight - viewportGap)),
    });
  }, [rowMenuEntry?.entry_id]);

  const loadDirectory = useCallback(async (parentId: string | null) => {
    const key = parentId || ROOT;
    const requestId = (directoryRequestIdsRef.current.get(key) || 0) + 1;
    directoryRequestIdsRef.current.set(key, requestId);
    setLoadingParents((current) => new Set(current).add(key));
    try {
      const response = await workspaceApi.listAllEntries({ parentId });
      if (directoryRequestIdsRef.current.get(key) !== requestId) return;
      const activeEntries = response.items.filter((entry) => entry.status === 'active');
      const revisions = reconcileAuthoritativeScope(
        directoryScopeRevisionsRef.current.get(key),
        activeEntries,
      );
      directoryScopeRevisionsRef.current.set(key, revisions);
      rememberAuthoritativeEntries(activeEntries);
      setChildren((current) => {
        const next = new Map(current);
        next.set(key, sortEntries(activeEntries));
        return next;
      });
      setError('');
    } catch (loadError) {
      if (directoryRequestIdsRef.current.get(key) !== requestId) return;
      setError(errorText(loadError));
    } finally {
      if (directoryRequestIdsRef.current.get(key) === requestId) {
        setLoadingParents((current) => { const next = new Set(current); next.delete(key); return next; });
      }
    }
  }, [reconcileAuthoritativeScope, rememberAuthoritativeEntries]);

  useEffect(() => {
    if (initialRootLoadStartedRef.current) return;
    initialRootLoadStartedRef.current = true;
    void loadDirectory(null);
  }, [loadDirectory]);
  useLayoutEffect(() => {
    const becameActive = isActive && !wasActiveRef.current;
    const becameInactive = !isActive && wasActiveRef.current;
    wasActiveRef.current = isActive;
    if (becameInactive) {
      requestRef.current += 1;
      setResults([]);
      setResultsQuery('');
      setSearching(false);
      return;
    }
    if (!becameActive) return;
    void loadDirectory(null);
    expanded.forEach((entryId) => void loadDirectory(entryId));
    if (query.trim()) {
      requestRef.current += 1;
      setResults([]);
      setResultsQuery('');
      setSearching(true);
      setSearchRefreshToken((current) => current + 1);
    }
  }, [expanded, isActive, loadDirectory, query]);
  useEffect(() => {
    if (!menuOpen && !searchOpen && !rowMenuEntry) return undefined;
    const closeOnOutside = (event: PointerEvent) => {
      const target = event.target as Node;
      if (!menuButtonRef.current?.contains(target) && !menuPopupRef.current?.contains(target)) setMenuOpen(false);
      if (!searchButtonRef.current?.contains(target) && !searchPopupRef.current?.contains(target)) setSearchOpen(false);
      if (!rowMenuButtonRef.current?.contains(target) && !rowMenuPopupRef.current?.contains(target)) {
        setRowMenuEntry(null);
        setRowMenuPosition(null);
      }
    };
    const closeOnEscape = (event: globalThis.KeyboardEvent) => {
      if (event.key !== 'Escape') return;
      if (rowMenuEntry) {
        setRowMenuEntry(null);
        setRowMenuPosition(null);
        rowMenuButtonRef.current?.focus();
      } else if (searchOpen) {
        setSearchOpen(false);
        searchButtonRef.current?.focus();
      } else if (menuOpen) {
        setMenuOpen(false);
        menuButtonRef.current?.focus();
      }
    };
    document.addEventListener('pointerdown', closeOnOutside);
    document.addEventListener('keydown', closeOnEscape);
    return () => {
      document.removeEventListener('pointerdown', closeOnOutside);
      document.removeEventListener('keydown', closeOnEscape);
    };
  }, [menuOpen, rowMenuEntry, searchOpen]);

  useEffect(() => {
    if (!menuOpen && !rowMenuEntry) return undefined;
    const closeMenu = () => {
      setMenuOpen(false);
      setRowMenuEntry(null);
      setRowMenuPosition(null);
    };
    window.addEventListener('resize', closeMenu);
    window.addEventListener('scroll', closeMenu, true);
    return () => {
      window.removeEventListener('resize', closeMenu);
      window.removeEventListener('scroll', closeMenu, true);
    };
  }, [menuOpen, rowMenuEntry]);

  useEffect(() => {
    const request = ++requestRef.current;
    const normalizedQuery = query.trim();
    setResults([]);
    setResultsQuery('');
    if (!normalizedQuery) {
      searchScopeRevisionsRef.current = null;
      setSearching(false);
      return () => {
        if (requestRef.current === request) requestRef.current += 1;
      };
    }
    setSearching(true);
    const timer = window.setTimeout(() => {
      void workspaceApi.listAllEntries({ query: normalizedQuery }).then((response) => {
        if (request === requestRef.current) {
          const activeEntries = response.items.filter((entry) => entry.status === 'active');
          const previous = searchScopeRevisionsRef.current?.query === normalizedQuery
            ? searchScopeRevisionsRef.current.revisions
            : undefined;
          searchScopeRevisionsRef.current = {
            query: normalizedQuery,
            revisions: reconcileAuthoritativeScope(previous, activeEntries),
          };
          rememberAuthoritativeEntries(activeEntries);
          setResults(activeEntries);
          setResultsQuery(normalizedQuery);
        }
      }).catch((searchError) => {
        if (request === requestRef.current) setError(errorText(searchError));
      }).finally(() => {
        if (request === requestRef.current) setSearching(false);
      });
    }, 180);
    return () => {
      window.clearTimeout(timer);
      if (requestRef.current === request) requestRef.current += 1;
    };
  }, [query, reconcileAuthoritativeScope, rememberAuthoritativeEntries, searchRefreshToken]);

  useEffect(() => subscribeWorkspaceMutation((detail) => {
    const tombstoneIds = detail.tombstone
      ? new Set(detail.affectedEntryIds || (detail.entryId ? [detail.entryId] : []))
      : null;
    if (tombstoneIds?.size) {
      forgetAuthoritativeEntries(tombstoneIds);
      directoryRequestIdsRef.current.forEach((requestId, key) => {
        directoryRequestIdsRef.current.set(key, requestId + 1);
      });
      requestRef.current += 1;
      setLoadingParents(new Set());
      setSearching(false);
      setSearchRefreshToken((current) => current + 1);
      setChildren((current) => {
        const next = new Map<string, WorkspaceEntry[]>();
        current.forEach((entries, key) => {
          if (tombstoneIds.has(key)) return;
          next.set(key, entries.filter((entry) => !tombstoneIds.has(entry.entry_id)));
        });
        return next;
      });
      setExpanded((current) => new Set([...current].filter((entryId) => !tombstoneIds.has(entryId))));
      setResults((current) => current.filter((entry) => !tombstoneIds.has(entry.entry_id)));
      setSelectedEntryIds((current) => new Set(
        [...current].filter((entryId) => !tombstoneIds.has(entryId)),
      ));
      setRowMenuEntry((current) => current && tombstoneIds.has(current.entry_id) ? null : current);
      setActionEntry((current) => current && tombstoneIds.has(current.entry_id) ? null : current);
      setDeleteEntry((current) => current && tombstoneIds.has(current.entry_id) ? null : current);
      setDraggingEntry((current) => current && tombstoneIds.has(current.entry_id) ? null : current);
      return;
    }
    if (!detail.entry) {
      if (detail.entryId) {
        void workspaceApi.getEntry(detail.entryId).then((entry) => {
          rememberAuthoritativeEntry(entry);
          emitWorkspaceMutation({ operation: detail.operation, entry, parentId: entry.parent_id });
        }).catch((loadError) => {
          if (loadError instanceof WorkspaceApiError && loadError.status === 404) {
            if (detail.entryId) forgetAuthoritativeEntries([detail.entryId]);
            setChildren((current) => {
              const next = new Map(current);
              current.forEach((entries, key) => next.set(key, entries.filter((entry) => entry.entry_id !== detail.entryId)));
              return next;
            });
            setResults((current) => current.filter((entry) => entry.entry_id !== detail.entryId));
            setSelectedEntryIds((current) => new Set(
              [...current].filter((entryId) => entryId !== detail.entryId),
            ));
            requestRef.current += 1;
            setSearching(false);
            setSearchRefreshToken((current) => current + 1);
          } else {
            setError('无法确认工作区条目状态，旧列表仍保留。');
          }
        });
      } else void loadDirectory(null);
      return;
    }
    rememberAuthoritativeEntry(detail.entry);
    directoryRequestIdsRef.current.forEach((requestId, key) => {
      directoryRequestIdsRef.current.set(key, requestId + 1);
    });
    setLoadingParents(new Set());
    requestRef.current += 1;
    setSearching(false);
    setSearchRefreshToken((current) => current + 1);
    setChildren((current) => {
      const next = new Map<string, WorkspaceEntry[]>();
      current.forEach((entries, key) => next.set(key, entries.filter((entry) => entry.entry_id !== detail.entry!.entry_id)));
      if (detail.entry!.status === 'active') {
        const key = detail.entry!.parent_id || ROOT;
        const loadedEntries = next.get(key);
        if (loadedEntries) next.set(key, sortEntries([...loadedEntries, detail.entry!]));
      }
      return next;
    });
    const refreshedEntry = detail.entry;
    setResults((current) => current.flatMap((entry) => (
      entry.entry_id !== refreshedEntry.entry_id
        ? [entry]
        : refreshedEntry.status === 'active' ? [refreshedEntry] : []
    )));
    if (refreshedEntry.status !== 'active') {
      setSelectedEntryIds((current) => new Set(
        [...current].filter((entryId) => entryId !== refreshedEntry.entry_id),
      ));
    }
    const refreshHeldEntry = (current: WorkspaceEntry | null) => (
      current?.entry_id === refreshedEntry.entry_id && current.revision < refreshedEntry.revision
        ? refreshedEntry
        : current
    );
    setRowMenuEntry(refreshHeldEntry);
    setActionEntry(refreshHeldEntry);
    setDeleteEntry(refreshHeldEntry);
    setDraggingEntry(refreshHeldEntry);
    if (
      draggingEntryRef.current?.entry_id === refreshedEntry.entry_id
      && draggingEntryRef.current.revision < refreshedEntry.revision
    ) {
      draggingEntryRef.current = refreshedEntry;
    }
  }), [
    forgetAuthoritativeEntries,
    loadDirectory,
    rememberAuthoritativeEntry,
  ]);

  const visibleTreeEntries = flattenVisibleEntries(children, expanded);
  const normalizedActiveQuery = query.trim();
  const activeSearchPending = Boolean(
    normalizedActiveQuery && resultsQuery !== normalizedActiveQuery,
  );
  const visibleActiveEntries = normalizedActiveQuery
    ? resultsQuery === normalizedActiveQuery ? results : []
    : visibleTreeEntries;
  const viewActiveEntriesById = new Map<string, WorkspaceEntry>();
  const indexCurrentEntry = (entry: WorkspaceEntry) => {
    if (entry.status !== 'active') return;
    const current = viewActiveEntriesById.get(entry.entry_id);
    if (!current || current.revision <= entry.revision) viewActiveEntriesById.set(entry.entry_id, entry);
  };
  children.forEach((entries) => entries.forEach(indexCurrentEntry));
  if (normalizedActiveQuery && resultsQuery === normalizedActiveQuery) {
    results.forEach(indexCurrentEntry);
  }
  const resolveAuthoritativeActiveEntry = (entryId: string) => {
    const authoritative = authoritativeEntriesRef.current.get(entryId);
    if (authoritative) return authoritative.status === 'active' ? authoritative : undefined;
    return viewActiveEntriesById.get(entryId);
  };
  const resolvedSelectedEntries = [...selectedEntryIds].flatMap((entryId) => {
    const entry = resolveAuthoritativeActiveEntry(entryId);
    return entry ? [entry] : [];
  });
  const unresolvedSelectedEntryCount = selectedEntryIds.size - resolvedSelectedEntries.length;
  const canonicalSelection = canonicalizeSelectedEntries(resolvedSelectedEntries);
  const ensureDeleteIntentKey = (entries: WorkspaceEntry[]) => {
    const signature = entryIntentSignature(entries);
    if (!deleteIntentRef.current || deleteIntentRef.current.signature !== signature) {
      deleteIntentRef.current = {
        signature,
        key: createWorkspaceIdempotencyKey('delete-batch'),
      };
    }
    return deleteIntentRef.current.key;
  };

  const clearDeleteIntent = () => {
    deleteIntentRef.current = null;
  };

  const clearSelection = () => {
    clearDeleteIntent();
    setSelectedEntryIds(new Set());
    setSelectionAnchorId(null);
  };

  const canonicalizeEntryIds = (entryIds: Iterable<string>) => {
    const unresolved: string[] = [];
    const resolved: WorkspaceEntry[] = [];
    [...entryIds].forEach((entryId) => {
      const entry = resolveAuthoritativeActiveEntry(entryId);
      if (entry) resolved.push(entry);
      else unresolved.push(entryId);
    });
    return new Set([
      ...unresolved,
      ...canonicalizeSelectedEntries(resolved).map((entry) => entry.entry_id),
    ]);
  };

  const toggleEntrySelection = (entry: WorkspaceEntry, range: boolean = false) => {
    clearDeleteIntent();
    const selectionEntry = resolveAuthoritativeActiveEntry(entry.entry_id) || entry;
    const anchorIndex = selectionAnchorId
      ? visibleActiveEntries.findIndex((item) => item.entry_id === selectionAnchorId)
      : -1;
    const targetIndex = visibleActiveEntries.findIndex((item) => item.entry_id === entry.entry_id);
    const validRange = range && anchorIndex >= 0 && targetIndex >= 0;
    setSelectedEntryIds((current) => {
      let next = new Set(current);
      if (validRange) {
        const start = Math.min(anchorIndex, targetIndex);
        const end = Math.max(anchorIndex, targetIndex);
        visibleActiveEntries.slice(start, end + 1).forEach((item) => next.add(item.entry_id));
      } else if (range) {
        next = new Set([entry.entry_id]);
      } else if (next.has(entry.entry_id)) {
        next.delete(entry.entry_id);
      } else if (![...next].some((entryId) => {
        const selected = resolveAuthoritativeActiveEntry(entryId);
        return Boolean(
          selected?.kind === 'directory' && isDescendantPath(selectionEntry.path, selected.path),
        );
      })) {
        if (selectionEntry.kind === 'directory') {
          next = new Set([...next].filter((entryId) => {
            const selected = resolveAuthoritativeActiveEntry(entryId);
            return !selected || !isDescendantPath(selected.path, selectionEntry.path);
          }));
        }
        next.add(entry.entry_id);
      }
      return canonicalizeEntryIds(next);
    });
    if (!range || !validRange) setSelectionAnchorId(entry.entry_id);
  };

  const selectCurrentView = () => {
    clearDeleteIntent();
    const canonical = canonicalizeSelectedEntries(visibleActiveEntries.map(
      (entry) => resolveAuthoritativeActiveEntry(entry.entry_id) || entry,
    ));
    setSelectedEntryIds(new Set(canonical.map((entry) => entry.entry_id)));
    setSelectionAnchorId(canonical[canonical.length - 1]?.entry_id || null);
  };

  const handleSelectionShortcut = (
    event: KeyboardEvent<HTMLElement>,
    entry: WorkspaceEntry,
    pending: boolean,
  ): boolean => {
    if ((event.ctrlKey || event.metaKey) && event.key.toLocaleLowerCase() === 'a') {
      event.preventDefault();
      event.stopPropagation();
      selectCurrentView();
      return true;
    }
    if (event.key === 'Escape') {
      event.preventDefault();
      event.stopPropagation();
      clearSelection();
      return true;
    }
    if (event.key === ' ') {
      event.preventDefault();
      event.stopPropagation();
      if (!pending) toggleEntrySelection(entry, event.shiftKey);
      return true;
    }
    return false;
  };

  const toggleDirectory = async (entry: WorkspaceEntry) => {
    if (expanded.has(entry.entry_id)) {
      setExpanded((current) => { const next = new Set(current); next.delete(entry.entry_id); return next; });
      return;
    }
    setExpanded((current) => new Set(current).add(entry.entry_id));
    if (!children.has(entry.entry_id)) await loadDirectory(entry.entry_id);
  };

  useEffect(() => {
    if (!activeEntryId) return undefined;
    let current = true;
    void workspaceApi.getEntry(activeEntryId).then(async (entry) => {
        if (!current) return;
        if (entry.kind !== 'directory' || entry.status !== 'active') return;
        const directoryIds = [entry.entry_id];
        if (entry.parent_id) directoryIds.push(entry.parent_id);
        setExpanded((current) => new Set([...current, ...directoryIds]));
        if (entry.parent_id) await loadDirectory(entry.parent_id);
        await loadDirectory(entry.entry_id);
      }).catch((loadError) => {
        if (!current) return;
        if (!(loadError instanceof WorkspaceApiError && loadError.status === 404)) {
          setError('无法打开工作区文件夹，请刷新后重试。');
        }
      });
    return () => { current = false; };
  }, [activeEntryId, loadDirectory]);

  const startCreateDirectory = (parent: WorkspaceEntry | null) => {
    if (parent && !canCreateSubdirectory(parent)) {
      setError('文件夹最多支持两层');
      setRowMenuEntry(null);
      setRowMenuPosition(null);
      return;
    }
    setCreateParent(parent);
    setActionEntry(null);
    setNameAction('directory');
    setNameDraft('');
    setMenuOpen(false);
    setRowMenuEntry(null);
    setRowMenuPosition(null);
  };

  const createFileDirectly = async (
    fileType: DirectFileType,
    parent: WorkspaceEntry | null,
  ) => {
    if (busy || directCreateInFlightRef.current) return;
    directCreateInFlightRef.current = true;
    setBusy(true);
    setMenuOpen(false);
    setRowMenuEntry(null);
    setRowMenuPosition(null);
    const parentId = parent?.entry_id || null;
    const parentKey = parentId || ROOT;
    const extension = fileType === 'markdown' ? 'md' : 'xlsx';
    const knownNames = new Set((children.get(parentKey) || []).map((entry) => entry.name));
    try {
      for (let sequence = 1; ; sequence += 1) {
        const name = sequence === 1 ? `未命名.${extension}` : `未命名 ${sequence}.${extension}`;
        if (knownNames.has(name)) continue;
        try {
          const result = await workspaceApi.createFile(parentId, name, fileType);
          rememberAuthoritativeEntry(result.entry);
          emitWorkspaceMutation({ operation: 'create', entry: result.entry, parentId: result.entry.parent_id });
          if (parent) {
            setExpanded((current) => new Set(current).add(parent.entry_id));
            void loadDirectory(parent.entry_id);
          }
          setError('');
          onOpenEntry(result.entry);
          return;
        } catch (createError) {
          if (
            createError instanceof WorkspaceApiError
            && createError.status === 409
            && createError.detail.code === 'NAME_CONFLICT'
          ) {
            knownNames.add(name);
            continue;
          }
          throw createError;
        }
      }
    } catch (createError) {
      setError(errorText(createError));
    } finally {
      directCreateInFlightRef.current = false;
      setBusy(false);
    }
  };

  const confirmName = async () => {
    if (!nameAction || !nameDraft.trim() || busy) return;
    const extension = nameAction === 'rename' && actionEntry?.kind === 'file'
      ? splitFileName(actionEntry.name).extension
      : '';
    const nextName = preserveFileExtension(nameDraft, extension);
    if (!nextName) return;
    setBusy(true);
    try {
      let result;
      if (nameAction === 'rename' && actionEntry) {
        result = await workspaceApi.updateEntry(actionEntry, { name: nextName });
      } else {
        result = await workspaceApi.createDirectory(createParent?.entry_id || null, nextName);
      }
      rememberAuthoritativeEntry(result.entry);
      emitWorkspaceMutation({ operation: nameAction === 'rename' ? 'rename' : 'create', entry: result.entry, parentId: result.entry.parent_id });
      if (nameAction === 'directory' && createParent) {
        setExpanded((current) => new Set(current).add(createParent.entry_id));
        void loadDirectory(createParent.entry_id);
      }
      setNameAction(null);
      setCreateParent(null);
      setError('');
    } catch (actionError) {
      const authoritativeEntry = revisionConflictEntry(actionError);
      if (authoritativeEntry) {
        rememberAuthoritativeEntry(authoritativeEntry);
        emitWorkspaceMutation({ operation: 'refresh', entry: authoritativeEntry, parentId: authoritativeEntry.parent_id });
        setError('条目内容刚刚更新，请确认名称后重试。');
      } else setError(errorText(actionError));
    } finally {
      setBusy(false);
    }
  };

  const uploadFiles = async (files: FileList | null) => {
    if (!files?.length) return;
    const parent = uploadParentRef.current;
    const parentId = parent?.entry_id || null;
    setBusy(true);
    try {
      for (const file of Array.from(files)) {
        const result = await workspaceApi.uploadFile(parentId, file);
        rememberAuthoritativeEntry(result.entry);
        emitWorkspaceMutation({ operation: 'upload', entry: result.entry, parentId: result.entry.parent_id });
      }
      if (parent) {
        setExpanded((current) => new Set(current).add(parent.entry_id));
        void loadDirectory(parent.entry_id);
      }
      setError('');
    } catch (uploadError) {
      setError(errorText(uploadError));
    } finally {
      setBusy(false);
      uploadParentRef.current = null;
      if (uploadRef.current) uploadRef.current.value = '';
    }
  };

  const openUploadPicker = (parent: WorkspaceEntry | null) => {
    uploadParentRef.current = parent;
    setMenuOpen(false);
    setRowMenuEntry(null);
    setRowMenuPosition(null);
    uploadRef.current?.click();
  };

  const refreshDirectory = (directory: WorkspaceEntry) => {
    setExpanded((current) => new Set(current).add(directory.entry_id));
    setRowMenuEntry(null);
    setRowMenuPosition(null);
    void loadDirectory(directory.entry_id);
  };

  const canDropInto = (source: WorkspaceEntry | null, targetDirectory: WorkspaceEntry | null) => {
    if (!source) return false;
    const targetParentId = targetDirectory?.entry_id || null;
    if (source.parent_id === targetParentId || source.entry_id === targetParentId) return false;
    if (
      source.kind === 'directory'
      && targetDirectory
      && targetDirectory.path.startsWith(`${source.path}/`)
    ) return false;
    return true;
  };

  const removePointerListeners = () => {
    const listeners = pointerListenersRef.current;
    if (!listeners) return;
    window.removeEventListener('pointermove', listeners.move);
    window.removeEventListener('pointerup', listeners.up);
    window.removeEventListener('pointercancel', listeners.cancel);
    pointerListenersRef.current = null;
  };

  const clearDragPreview = () => {
    if (dragPreviewFrameRef.current !== null) window.cancelAnimationFrame(dragPreviewFrameRef.current);
    dragPreviewFrameRef.current = null;
    setDragPreviewPoint(null);
  };

  const scheduleDragPreview = (x: number, y: number) => {
    dragPreviewPointRef.current = { x, y };
    if (dragPreviewFrameRef.current !== null) return;
    dragPreviewFrameRef.current = window.requestAnimationFrame(() => {
      dragPreviewFrameRef.current = null;
      setDragPreviewPoint(dragPreviewPointRef.current);
    });
  };

  const finishDrag = () => {
    removePointerListeners();
    clearDragPreview();
    pointerDragRef.current = null;
    pointerDropTargetIdRef.current = null;
    draggingEntryRef.current = null;
    setDraggingEntry(null);
    setDropTargetId(null);
  };

  const loadedEntry = (entryId: string): WorkspaceEntry | null => {
    for (const entries of children.values()) {
      const entry = entries.find((item) => item.entry_id === entryId);
      if (entry) return entry;
    }
    return null;
  };

  const startPointerDrag = (event: ReactPointerEvent<HTMLElement>, entry: WorkspaceEntry) => {
    if ((event.button != null && event.button !== 0) || busy) return;
    removePointerListeners();
    clearDragPreview();
    pointerDragRef.current = {
      entry,
      pointerId: event.pointerId || 1,
      startX: event.clientX || 0,
      startY: event.clientY || 0,
      active: false,
    };
    pointerDropTargetIdRef.current = null;
    const listeners = {
      move: (pointerEvent: globalThis.PointerEvent) => movePointerDrag(pointerEvent),
      up: (pointerEvent: globalThis.PointerEvent) => finishPointerDrag(pointerEvent),
      cancel: (pointerEvent: globalThis.PointerEvent) => cancelPointerDrag(pointerEvent),
    };
    pointerListenersRef.current = listeners;
    window.addEventListener('pointermove', listeners.move, { passive: false });
    window.addEventListener('pointerup', listeners.up);
    window.addEventListener('pointercancel', listeners.cancel);
  };

  const movePointerDrag = (event: globalThis.PointerEvent) => {
    const pending = pointerDragRef.current;
    const pointerId = event.pointerId || 1;
    if (!pending || pending.pointerId !== pointerId) return;
    if (!pending.active) {
      const hasCoordinates = Number.isFinite(event.clientX) && Number.isFinite(event.clientY);
      const distance = hasCoordinates ? Math.hypot(event.clientX - pending.startX, event.clientY - pending.startY) : 6;
      if (distance < 6) return;
      pending.active = true;
      draggingEntryRef.current = pending.entry;
      setDraggingEntry(pending.entry);
      setDropTargetId(null);
      setRowMenuEntry(null);
      setRowMenuPosition(null);
      dragPreviewPointRef.current = { x: event.clientX, y: event.clientY };
      setDragPreviewPoint(dragPreviewPointRef.current);
    } else {
      scheduleDragPreview(event.clientX, event.clientY);
    }
    event.preventDefault();
    const targetElement = document.elementFromPoint(event.clientX, event.clientY)?.closest<HTMLElement>('[data-workspace-drop-target]');
    const targetId = targetElement?.dataset.workspaceDropTarget || null;
    const targetDirectory = targetId && targetId !== ROOT ? loadedEntry(targetId) : null;
    if (!targetId || (targetId !== ROOT && !targetDirectory) || !canDropInto(pending.entry, targetDirectory)) {
      pointerDropTargetIdRef.current = null;
      setDropTargetId(null);
      return;
    }
    pointerDropTargetIdRef.current = targetId;
    setDropTargetId(targetId);
  };

  const moveByDrag = async (targetDirectory: WorkspaceEntry | null) => {
    const source = draggingEntryRef.current;
    if (!source || busy || !canDropInto(source, targetDirectory)) {
      finishDrag();
      return;
    }
    setBusy(true);
    try {
      const result = await workspaceApi.updateEntry(source, {
        parentId: targetDirectory?.entry_id || null,
      });
      rememberAuthoritativeEntry(result.entry);
      emitWorkspaceMutation({ operation: 'move', entry: result.entry, parentId: result.entry.parent_id });
      if (targetDirectory) {
        setExpanded((current) => new Set(current).add(targetDirectory.entry_id));
        if (!children.has(targetDirectory.entry_id)) {
          void loadDirectory(targetDirectory.entry_id);
        }
      }
      setError('');
    } catch (moveError) {
      const authoritativeEntry = revisionConflictEntry(moveError);
      if (authoritativeEntry) {
        rememberAuthoritativeEntry(authoritativeEntry);
        emitWorkspaceMutation({ operation: 'refresh', entry: authoritativeEntry, parentId: authoritativeEntry.parent_id });
        setError('条目内容刚刚更新，请重新拖动。');
      } else setError(errorText(moveError));
    } finally {
      setBusy(false);
      finishDrag();
    }
  };

  const finishPointerDrag = (event: globalThis.PointerEvent) => {
    const pending = pointerDragRef.current;
    const pointerId = event.pointerId || 1;
    if (!pending || pending.pointerId !== pointerId) return;
    removePointerListeners();
    clearDragPreview();
    const targetId = pointerDropTargetIdRef.current;
    pointerDragRef.current = null;
    pointerDropTargetIdRef.current = null;
    if (!pending.active) return;
    suppressClickEntryIdRef.current = pending.entry.entry_id;
    window.setTimeout(() => {
      if (suppressClickEntryIdRef.current === pending.entry.entry_id) suppressClickEntryIdRef.current = null;
    }, 0);
    const targetDirectory = targetId && targetId !== ROOT ? loadedEntry(targetId) : null;
    if (!targetId || (targetId !== ROOT && !targetDirectory)) {
      finishDrag();
      return;
    }
    void moveByDrag(targetDirectory);
  };

  const cancelPointerDrag = (event: globalThis.PointerEvent) => {
    const pointerId = event.pointerId || 1;
    if (pointerDragRef.current?.pointerId !== pointerId) return;
    finishDrag();
  };

  const deleteActiveEntries = async (requestedEntryIds: Iterable<string>): Promise<boolean> => {
    if (activeSearchPending || searching) {
      setError('正在刷新工作区状态，请稍候。');
      return false;
    }
    const entryIds = [...new Set(requestedEntryIds)];
    const resolveRoots = () => {
      const resolved = entryIds.flatMap((entryId) => {
        const entry = authoritativeEntriesRef.current.get(entryId)
          || viewActiveEntriesById.get(entryId);
        return entry?.status === 'active' ? [entry] : [];
      });
      return {
        missingEntryIds: entryIds.filter((entryId) => !resolved.some((entry) => entry.entry_id === entryId)),
        roots: canonicalizeSelectedEntries(resolved),
      };
    };
    const { missingEntryIds, roots } = resolveRoots();
    if (missingEntryIds.length > 0) {
      setError('所选条目状态已失效，请刷新目录或重新搜索后重试。');
      return false;
    }
    if (roots.length === 0 || batchPendingIds.size > 0) return false;
    if (roots.length > 200) {
      setError('每次最多可将 200 个根条目删除，请减少选择后重试。');
      return false;
    }
    ensureDeleteIntentKey(roots);
    setBatchPendingIds(new Set(roots.map((entry) => entry.entry_id)));
    setError('');
    try {
      const refreshedIdempotencyKey = ensureDeleteIntentKey(roots);
      setBatchPendingIds(new Set(roots.map((entry) => entry.entry_id)));
      const result = await workspaceApi.deleteEntries(roots, refreshedIdempotencyKey);
      clearDeleteIntent();
      void discardWorkspaceDrafts(result.affected_entry_ids);
      emitWorkspaceMutation({
        operation: 'delete',
        affectedEntryIds: result.affected_entry_ids,
        tombstone: true,
        origin: 'local',
      });
      return true;
    } catch (deleteError) {
      const authoritativeEntry = revisionConflictEntry(deleteError);
      if (authoritativeEntry) {
        rememberAuthoritativeEntry(authoritativeEntry);
        emitWorkspaceMutation({ operation: 'refresh', entry: authoritativeEntry, parentId: authoritativeEntry.parent_id });
        setError('所选条目刚刚更新，已保留选择，请再次确认。');
      } else setError(errorText(deleteError));
      return false;
    } finally {
      setBatchPendingIds(new Set());
    }
  };

  const confirmBatchDelete = async () => {
    const success = await deleteActiveEntries(selectedEntryIds);
    setBatchDeleteConfirmOpen(false);
    if (success) setSelectionAnchorId(null);
  };

  const confirmDelete = async () => {
    if (!deleteEntry || busy) return;
    if (await deleteActiveEntries([deleteEntry.entry_id])) setDeleteEntry(null);
  };

  const renderEntry = (entry: WorkspaceEntry): React.ReactNode => {
    const directory = entry.kind === 'directory';
    const opened = directory && expanded.has(entry.entry_id);
    const selected = selectedEntryIds.has(entry.entry_id);
    const pending = batchPendingIds.has(entry.entry_id);
    const fileInfo = workspaceEntryToFileInfo(entry);
    const Icon = getFileIcon(fileInfo);
    const handleKeyDown = (event: KeyboardEvent<HTMLElement>) => {
      if (handleSelectionShortcut(event, entry, pending)) return;
      const buttons = Array.from(event.currentTarget.closest('[role="tree"]')?.querySelectorAll<HTMLElement>('[data-workspace-sidebar-tree-entry]') || []);
      const index = buttons.indexOf(event.currentTarget);
      if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
        event.preventDefault();
        buttons[event.key === 'ArrowDown' ? Math.min(buttons.length - 1, index + 1) : Math.max(0, index - 1)]?.focus();
      } else if (event.key === 'ArrowRight' && directory) {
        event.preventDefault(); if (!opened) void toggleDirectory(entry);
      } else if (event.key === 'ArrowLeft' && directory && opened) {
        event.preventDefault(); void toggleDirectory(entry);
      } else if (event.key === 'Enter') {
        event.preventDefault(); directory ? void toggleDirectory(entry) : onOpenEntry(entry);
      }
    };
    return (
      <li key={entry.entry_id} role="treeitem" aria-expanded={directory ? opened : undefined} aria-selected={selected} aria-busy={pending || undefined}>
        <div
          data-testid={`workspace-drag-row-${entry.entry_id}`}
          data-workspace-drop-target={directory ? entry.entry_id : entry.parent_id || undefined}
          className={`group/entry mx-1 flex min-h-10 items-center rounded-lg pl-1 transition-[background-color,box-shadow,opacity] ${
            draggingEntry?.entry_id === entry.entry_id ? 'opacity-45' : ''
          } ${
            dropTargetId === entry.entry_id ? 'bg-claude-accent/10 ring-1 ring-inset ring-claude-accent/35' : ''
          } ${selected ? 'bg-claude-accent/10 text-claude-text ring-1 ring-inset ring-claude-accent/25' : activeEntryId === entry.entry_id ? 'bg-white text-claude-text shadow-sm' : 'text-claude-secondary hover:bg-claude-hover'}`}
        >
          {directory ? <button type="button" onClick={() => void toggleDirectory(entry)} className="inline-flex h-9 w-5 shrink-0 items-center justify-center rounded focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-claude-accent/40" aria-label={`${opened ? '收起' : '展开'} ${entry.name}`}>{loadingParents.has(entry.entry_id) ? <Loader2 size={13} className="animate-spin" /> : opened ? <ChevronDown size={13} /> : <ChevronRight size={13} />}</button> : <span className="h-9 w-5 shrink-0" aria-hidden="true" />}
          <div className="relative mr-1 flex h-9 w-6 shrink-0 items-center justify-center">
            <Icon size={15} className={`shrink-0 ${getFileIconClass(fileInfo)} ${selectedEntryIds.size > 0 ? 'opacity-0' : 'opacity-0 md:opacity-100 md:group-hover/entry:opacity-0 md:group-focus-within/entry:opacity-0'}`} aria-hidden="true" />
            <label className={`absolute inset-0 flex cursor-pointer items-center justify-center ${selectedEntryIds.size > 0 ? 'opacity-100' : 'opacity-100 md:opacity-0 md:group-hover/entry:opacity-100 md:group-focus-within/entry:opacity-100'}`}>
              <input type="checkbox" checked={selected} readOnly disabled={pending} onKeyDown={(event) => { handleSelectionShortcut(event, entry, pending); }} onClick={(event) => { event.stopPropagation(); toggleEntrySelection(entry, event.shiftKey); }} className="h-4 w-4 cursor-pointer rounded border-claude-border accent-claude-accent disabled:cursor-wait" aria-label={`选择 ${entry.name}`} />
            </label>
          </div>
          <div role="button" tabIndex={0} title={entry.name} aria-selected={selected} aria-current={activeEntryId === entry.entry_id ? 'page' : undefined} aria-grabbed={draggingEntry?.entry_id === entry.entry_id} onPointerDown={(event) => startPointerDrag(event, entry)} data-workspace-pointer-drag-source data-workspace-sidebar-tree-entry onKeyDown={handleKeyDown} onClick={(event) => { if (suppressClickEntryIdRef.current === entry.entry_id) { suppressClickEntryIdRef.current = null; return; } if (event.shiftKey || event.ctrlKey || event.metaKey) { toggleEntrySelection(entry, event.shiftKey); return; } directory ? void toggleDirectory(entry) : onOpenEntry(entry); }} className={`flex min-w-0 flex-1 touch-none select-none items-center gap-2 self-stretch text-left text-[13px] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-claude-accent/40 ${busy ? '' : 'cursor-grab active:cursor-grabbing'}`}><span className="min-w-0 flex-1 truncate">{entry.name}</span>{pending && <Loader2 size={13} className="mr-1 shrink-0 animate-spin text-claude-accent" aria-label="正在删除" />}</div>
          <button type="button" draggable={false} onClick={(event) => openRowMenu(entry, event.currentTarget)} className="mr-3 inline-flex h-8 w-8 items-center justify-center rounded-md text-claude-muted opacity-65 hover:bg-white hover:opacity-100 focus-visible:opacity-100 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-claude-accent/40" aria-label={`${entry.name} 操作`} aria-expanded={rowMenuEntry?.entry_id === entry.entry_id}><MoreHorizontal size={14} /></button>
        </div>
        {opened && <ul role="group" data-workspace-drop-target={entry.entry_id} className={`ml-2 rounded-lg transition-colors ${dropTargetId === entry.entry_id ? 'bg-claude-accent/[0.035]' : ''}`}>{(children.get(entry.entry_id) || []).map((child) => renderEntry(child))}</ul>}
      </li>
    );
  };

  const renderSearchEntry = (entry: WorkspaceEntry) => {
    const selected = selectedEntryIds.has(entry.entry_id);
    const pending = batchPendingIds.has(entry.entry_id);
    const handleKeyDown = (event: KeyboardEvent<HTMLElement>) => {
      if (handleSelectionShortcut(event, entry, pending)) return;
      const rows = Array.from(event.currentTarget.closest('[data-workspace-search-results]')?.querySelectorAll<HTMLElement>('[data-workspace-sidebar-search-entry]') || []);
      const index = rows.indexOf(event.currentTarget);
      if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
        event.preventDefault();
        rows[event.key === 'ArrowDown' ? Math.min(rows.length - 1, index + 1) : Math.max(0, index - 1)]?.focus();
      } else if (event.key === 'Enter') {
        event.preventDefault(); entry.kind === 'file' ? onOpenEntry(entry) : void toggleDirectory(entry);
      }
    };
    return (
      <div key={entry.entry_id} aria-busy={pending || undefined} className={`group mx-1 flex min-h-10 items-center rounded-lg px-1 ${selected ? 'bg-claude-accent/10 ring-1 ring-inset ring-claude-accent/25' : 'hover:bg-claude-hover'}`}>
        <label className={`flex h-9 w-7 shrink-0 cursor-pointer items-center justify-center transition-opacity ${selectedEntryIds.size > 0 || selected ? 'opacity-100' : 'opacity-100 md:opacity-0 md:group-hover:opacity-100 md:group-focus-within:opacity-100'}`}><input type="checkbox" checked={selected} readOnly disabled={pending} onKeyDown={(event) => { handleSelectionShortcut(event, entry, pending); }} onClick={(event) => { event.stopPropagation(); toggleEntrySelection(entry, event.shiftKey); }} className="h-4 w-4 cursor-pointer rounded border-claude-border accent-claude-accent disabled:cursor-wait" aria-label={`选择 ${entry.name}`} /></label>
        <button type="button" role="option" aria-selected={selected} title={entry.path} data-workspace-sidebar-search-entry onKeyDown={handleKeyDown} onClick={(event) => { if (event.shiftKey || event.ctrlKey || event.metaKey) { toggleEntrySelection(entry, event.shiftKey); return; } entry.kind === 'file' ? onOpenEntry(entry) : void toggleDirectory(entry); }} className="flex min-h-10 min-w-0 flex-1 items-center gap-2 rounded-lg px-1 text-left text-xs focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-claude-accent/40"><span className="min-w-0 flex-1 truncate">{entry.path}</span>{pending && <Loader2 size={13} className="shrink-0 animate-spin text-claude-accent" aria-label="正在删除" />}</button>
      </div>
    );
  };

  const visible = visibleActiveEntries;
  return (
    <div className="relative flex min-h-0 min-w-0 w-full flex-1 flex-col" data-testid="workspace-sidebar-content">
      <div data-testid="workspace-panel-header" className="pointer-events-none absolute inset-x-0 -top-11 z-10 flex h-11 items-center justify-end px-1">
        <div className="pointer-events-auto flex items-center gap-0.5">
          <button ref={searchButtonRef} type="button" onClick={() => { setSearchOpen((open) => !open); setMenuOpen(false); }} className="inline-flex h-8 w-8 items-center justify-center rounded-md text-claude-muted hover:bg-claude-hover hover:text-claude-text focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-claude-accent/40" aria-label="搜索工作区文件" aria-expanded={searchOpen}><Search size={16} /></button>
          <button ref={menuButtonRef} type="button" onClick={() => { setSearchOpen(false); if (menuOpen) setMenuOpen(false); else { positionMenuBesideSidebar(); setMenuOpen(true); } }} className="inline-flex h-8 w-8 items-center justify-center rounded-md text-claude-muted hover:bg-claude-hover hover:text-claude-text focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-claude-accent/40" aria-label="工作区操作" aria-expanded={menuOpen}><MoreHorizontal size={17} /></button>
        </div>
        {searchOpen && <div ref={searchPopupRef} className="pointer-events-auto absolute left-1 right-1 top-full z-20 mt-1 rounded-xl border border-claude-border bg-white p-2 shadow-[0_10px_28px_rgba(30,26,20,0.12)] transition motion-reduce:transition-none"><label className="relative block"><Search size={14} className="pointer-events-none absolute left-2.5 top-1/2 -translate-y-1/2 text-claude-muted" /><span className="sr-only">搜索工作区</span><input autoFocus value={query} onChange={(event) => setQuery(event.target.value)} placeholder="搜索名称或路径" className="h-9 w-full rounded-lg border border-claude-border bg-white pl-8 pr-8 text-xs text-claude-text outline-none focus:border-claude-accent focus:ring-2 focus:ring-claude-accent/20" />{searching && <Loader2 size={13} className="absolute right-2.5 top-1/2 -translate-y-1/2 animate-spin text-claude-muted" />}{query && !searching && <button type="button" onClick={() => setQuery('')} className="absolute right-1.5 top-1/2 -translate-y-1/2 rounded p-1 text-claude-muted hover:bg-claude-hover" aria-label="清空工作区搜索"><X size={12} /></button>}</label></div>}
        {menuOpen && menuPosition && createPortal(
          <div
            ref={menuPopupRef}
            role="menu"
            aria-label="工作区根目录操作菜单"
            data-testid="workspace-actions-menu"
            style={{ left: menuPosition.left, top: menuPosition.top }}
            className="pointer-events-auto fixed z-[220] w-[172px] origin-top-left rounded-xl border border-claude-border bg-white p-1 shadow-[0_10px_26px_rgba(30,26,20,0.14)] transition duration-150 motion-reduce:transition-none"
          >
            <button type="button" role="menuitem" disabled={busy} className="flex h-9 w-full items-center gap-2 rounded-lg px-2.5 text-left text-[12px] leading-none text-claude-text hover:bg-claude-hover focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-claude-accent/40 disabled:cursor-wait disabled:opacity-45" onClick={() => startCreateDirectory(null)}><FolderPlus size={14} aria-hidden="true" />新建文件夹</button>
            <button type="button" role="menuitem" disabled={busy} className="flex h-9 w-full items-center gap-2 rounded-lg px-2.5 text-left text-[12px] leading-none text-claude-text hover:bg-claude-hover focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-claude-accent/40 disabled:cursor-wait disabled:opacity-45" onClick={() => void createFileDirectly('markdown', null)}><FilePlus2 size={14} aria-hidden="true" />新建 Markdown</button>
            <button type="button" role="menuitem" disabled={busy} className="flex h-9 w-full items-center gap-2 rounded-lg px-2.5 text-left text-[12px] leading-none text-claude-text hover:bg-claude-hover focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-claude-accent/40 disabled:cursor-wait disabled:opacity-45" onClick={() => void createFileDirectly('xlsx', null)}><FileSpreadsheet size={14} aria-hidden="true" />新建表格</button>
            <button type="button" role="menuitem" disabled={busy} className="flex h-9 w-full items-center gap-2 rounded-lg px-2.5 text-left text-[12px] leading-none text-claude-text hover:bg-claude-hover focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-claude-accent/40 disabled:cursor-wait disabled:opacity-45" onClick={() => openUploadPicker(null)}><Upload size={14} aria-hidden="true" />上传文件</button>
            <button type="button" role="menuitem" disabled={busy} className="flex h-9 w-full items-center gap-2 rounded-lg px-2.5 text-left text-[12px] leading-none text-claude-text hover:bg-claude-hover focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-claude-accent/40 disabled:cursor-wait disabled:opacity-45" onClick={() => { setMenuOpen(false); void loadDirectory(null); }}><RefreshCw size={14} aria-hidden="true" />刷新</button>
          </div>,
          document.body,
        )}
      </div>
      {rowMenuEntry && rowMenuPosition && createPortal(
        <div
          ref={rowMenuPopupRef}
          role="menu"
          aria-label={`${rowMenuEntry.name} 操作菜单`}
          data-testid="workspace-row-actions-menu"
          style={{ left: rowMenuPosition.left, top: rowMenuPosition.top }}
          className={`fixed z-[221] rounded-xl border border-claude-border bg-white p-1 shadow-[0_10px_26px_rgba(30,26,20,0.14)] motion-reduce:transition-none ${rowMenuEntry.kind === 'directory' ? 'w-[172px]' : 'w-[140px]'}`}
        >
          {rowMenuEntry.kind === 'directory' && (
            <>
              {canCreateSubdirectory(rowMenuEntry) && <button type="button" role="menuitem" disabled={busy} className="flex h-9 w-full items-center gap-2 rounded-lg px-2.5 text-left text-[12px] leading-none text-claude-text hover:bg-claude-hover focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-claude-accent/40 disabled:cursor-wait disabled:opacity-45" onClick={() => startCreateDirectory(rowMenuEntry)}><FolderPlus size={14} aria-hidden="true" />新建文件夹</button>}
              <button type="button" role="menuitem" disabled={busy} className="flex h-9 w-full items-center gap-2 rounded-lg px-2.5 text-left text-[12px] leading-none text-claude-text hover:bg-claude-hover focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-claude-accent/40 disabled:cursor-wait disabled:opacity-45" onClick={() => void createFileDirectly('markdown', rowMenuEntry)}><FilePlus2 size={14} aria-hidden="true" />新建 Markdown</button>
              <button type="button" role="menuitem" disabled={busy} className="flex h-9 w-full items-center gap-2 rounded-lg px-2.5 text-left text-[12px] leading-none text-claude-text hover:bg-claude-hover focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-claude-accent/40 disabled:cursor-wait disabled:opacity-45" onClick={() => void createFileDirectly('xlsx', rowMenuEntry)}><FileSpreadsheet size={14} aria-hidden="true" />新建表格</button>
              <button type="button" role="menuitem" disabled={busy} className="flex h-9 w-full items-center gap-2 rounded-lg px-2.5 text-left text-[12px] leading-none text-claude-text hover:bg-claude-hover focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-claude-accent/40 disabled:cursor-wait disabled:opacity-45" onClick={() => openUploadPicker(rowMenuEntry)}><Upload size={14} aria-hidden="true" />上传文件</button>
              <button type="button" role="menuitem" disabled={busy} className="flex h-9 w-full items-center gap-2 rounded-lg px-2.5 text-left text-[12px] leading-none text-claude-text hover:bg-claude-hover focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-claude-accent/40 disabled:cursor-wait disabled:opacity-45" onClick={() => refreshDirectory(rowMenuEntry)}><RefreshCw size={14} aria-hidden="true" />刷新</button>
              <div role="separator" className="mx-1 my-1 border-t border-claude-border" />
            </>
          )}
          <button
            type="button"
            role="menuitem"
            className="flex h-9 w-full items-center gap-2 rounded-lg px-2.5 text-left text-[12px] text-claude-text hover:bg-claude-hover focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-claude-accent/40"
            onClick={() => {
              const editableName = rowMenuEntry.kind === 'file'
                ? splitFileName(rowMenuEntry.name).stem
                : rowMenuEntry.name;
              setCreateParent(null);
              setActionEntry(rowMenuEntry);
              setNameDraft(editableName);
              setNameAction('rename');
              setRowMenuEntry(null);
              setRowMenuPosition(null);
            }}
          >
            <Pencil size={14} aria-hidden="true" />重命名
          </button>
          <button
            type="button"
            role="menuitem"
            className="flex h-9 w-full items-center gap-2 rounded-lg px-2.5 text-left text-[12px] text-claude-error hover:bg-red-50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-claude-error/30"
            onClick={() => {
              ensureDeleteIntentKey([rowMenuEntry]);
              setDeleteEntry(rowMenuEntry);
              setRowMenuEntry(null);
              setRowMenuPosition(null);
            }}
          >
            <Trash2 size={14} aria-hidden="true" />删除
          </button>
        </div>,
        document.body,
      )}
      {draggingEntry && dragPreviewPoint && (() => {
        const previewInfo = workspaceEntryToFileInfo(draggingEntry);
        const PreviewIcon = getFileIcon(previewInfo);
        const left = Math.max(8, Math.min(dragPreviewPoint.x + 14, window.innerWidth - 274));
        const top = Math.max(8, Math.min(dragPreviewPoint.y + 14, window.innerHeight - 54));
        return createPortal(
          <div
            data-testid="workspace-drag-preview"
            aria-hidden="true"
            style={{ transform: `translate3d(${left}px, ${top}px, 0)` }}
            className="pointer-events-none fixed left-0 top-0 z-[240] flex h-10 max-w-[260px] items-center gap-2 rounded-lg border border-claude-border bg-white/95 px-3 text-[13px] text-claude-text opacity-95 shadow-[0_8px_22px_rgba(30,26,20,0.18)] backdrop-blur-sm will-change-transform"
          >
            <PreviewIcon size={15} className={`shrink-0 ${getFileIconClass(previewInfo)}`} />
            <span className="min-w-0 flex-1 truncate font-medium">{draggingEntry.name}</span>
          </div>,
          document.body,
        );
      })()}
      <input ref={uploadRef} type="file" multiple className="hidden" onChange={(event) => void uploadFiles(event.target.files)} />
      {error && <div className="mx-1 mb-2 flex items-center gap-2 rounded-lg bg-red-50 px-2.5 py-2 text-[11px] text-claude-error" role="alert"><span className="min-w-0 flex-1 whitespace-normal">{error}</span><button type="button" onClick={() => void loadDirectory(null)} disabled={loadingParents.has(ROOT) || searching} className="shrink-0 rounded-md px-1.5 py-1 font-medium hover:bg-white/70 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-claude-error/30 disabled:opacity-45" aria-label="重试加载工作区">重试</button><button type="button" onClick={() => setError('')} className="shrink-0 rounded p-1 hover:bg-white/70 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-claude-error/30" aria-label="关闭错误"><X size={12} /></button></div>}
      <div
        data-testid="workspace-content-body"
        className={`flex min-h-0 min-w-0 w-full flex-1 flex-col px-1 pb-2 ${dropTargetId === ROOT ? 'bg-claude-accent/[0.04]' : ''}`}
      >
        {draggingEntry && canDropInto(draggingEntry, null) && (
          <div
            data-testid="workspace-root-drop-zone"
            data-workspace-drop-target={ROOT}
            className={`mx-1 mb-1 rounded-lg border border-dashed px-2 py-1.5 text-center text-[10px] ${
              dropTargetId === ROOT
                ? 'border-claude-accent bg-claude-accent/10 text-claude-accent'
                : 'border-claude-border text-claude-muted'
            }`}
          >
            移到工作区根目录
          </div>
        )}
        {visible.length === 0 && !searching && !activeSearchPending ? (
          <div data-testid="workspace-empty-state" className="flex min-h-0 flex-1 flex-col items-center justify-center px-4 text-center text-xs text-claude-muted">
            <Folder size={30} className="mb-3 text-claude-border" aria-hidden="true" />
            <span className="font-medium text-claude-secondary">{query ? '没有匹配文件' : '工作区为空'}</span>
            {!query && <span className="mt-1.5 block max-w-[180px] whitespace-normal leading-5">可新建文件，或从会话文件面板存入内容</span>}
          </div>
        ) : (
          <div className="min-h-0 min-w-0 w-full flex-1 overflow-x-hidden overflow-y-auto">
            {query.trim() ? <div className="space-y-1" role="listbox" aria-label="工作区搜索结果" aria-multiselectable="true" data-workspace-search-results>{visible.map((entry) => renderSearchEntry(entry))}</div> : <ul role="tree" aria-label="工作区文件树" aria-multiselectable="true" className="min-w-0 w-full">{(children.get(ROOT) || []).map((entry) => renderEntry(entry))}</ul>}
          </div>
        )}
      </div>
      {selectedEntryIds.size > 0 && <div className="sticky bottom-0 z-10 mx-1 mb-1 flex min-h-11 shrink-0 items-center gap-2 rounded-xl border border-claude-border bg-white px-2.5 py-1.5 shadow-[0_-4px_16px_rgba(30,26,20,0.08)]"><span className="min-w-0 flex-1 text-[11px] font-medium text-claude-secondary">已选 {selectedEntryIds.size}</span>{(activeSearchPending || searching) && <span className="text-[10px] text-claude-muted" role="status">正在刷新状态</span>}{unresolvedSelectedEntryCount > 0 && <span className="text-[10px] text-claude-error" role="alert">{unresolvedSelectedEntryCount} 项状态已失效</span>}{canonicalSelection.length > 200 && <span className="text-[10px] text-claude-error" role="alert">每次最多 200 项</span>}<button type="button" onClick={clearSelection} disabled={batchPendingIds.size > 0} className="h-8 rounded-md px-2 text-[11px] text-claude-muted hover:bg-claude-hover disabled:opacity-40">清除</button><button type="button" onClick={() => { ensureDeleteIntentKey(canonicalSelection); setBatchDeleteConfirmOpen(true); }} disabled={batchPendingIds.size > 0 || activeSearchPending || searching || unresolvedSelectedEntryCount > 0 || canonicalSelection.length > 200} className="h-8 rounded-md bg-claude-accent px-2.5 text-[11px] font-semibold text-white disabled:cursor-wait disabled:opacity-40">{batchPendingIds.size > 0 ? '处理中…' : '删除'}</button></div>}
      <div className="border-t border-claude-border px-2 py-2 text-[10px] text-claude-muted">顶部新建位置：工作区根目录</div>

      {nameAction && <SidebarNameDialog title={nameAction === 'rename' ? '重命名' : '新建文件夹'} value={nameDraft} suffix={nameAction === 'rename' && actionEntry?.kind === 'file' ? splitFileName(actionEntry.name).extension : ''} busy={busy} returnFocusRef={nameAction === 'rename' || createParent ? rowMenuButtonRef : menuButtonRef} onChange={setNameDraft} onClose={() => { setNameAction(null); setCreateParent(null); }} onConfirm={() => void confirmName()} />}
      {deleteEntry && <ConfirmDialog title={`删除“${deleteEntry.name}”？`} description="文件及其历史版本会永久删除，本地未保存修改也将丢弃，无法恢复。" confirmLabel="删除" busyLabel="正在删除…" busy={busy || batchPendingIds.has(deleteEntry.entry_id)} onCancel={() => { clearDeleteIntent(); setDeleteEntry(null); }} onConfirm={() => void confirmDelete()} />}
      {batchDeleteConfirmOpen && <ConfirmDialog title={`将所选 ${selectedEntryIds.size} 项删除？`} description="所选文件夹及其全部后代、历史版本和本地未保存修改会永久删除，无法恢复。" confirmLabel="删除" busyLabel="正在删除…" busy={batchPendingIds.size > 0} onCancel={() => { clearDeleteIntent(); setBatchDeleteConfirmOpen(false); }} onConfirm={() => void confirmBatchDelete()} />}
    </div>
  );
}

function SidebarNameDialog({ title, value, suffix, busy, returnFocusRef, onChange, onClose, onConfirm }: { title: string; value: string; suffix?: string; busy: boolean; returnFocusRef: React.RefObject<HTMLElement>; onChange: (value: string) => void; onClose: () => void; onConfirm: () => void }) {
  const normalizedValue = preserveFileExtension(value, suffix || '');
  return (
    <SidebarDialogShell label={title} busy={busy} returnFocusRef={returnFocusRef} onClose={onClose} panelClassName="max-w-sm">
      <h2 className="text-[15px] font-semibold leading-6 text-claude-text">{title}</h2>
      <div className="mt-3.5 text-[12px] font-medium text-claude-secondary">
        <label htmlFor="workspace-sidebar-name-input" className="block">名称</label>
        <span className="mt-1.5 flex h-10 w-full items-center rounded-lg border border-claude-border bg-white focus-within:border-claude-accent focus-within:ring-2 focus-within:ring-claude-accent/25">
          <input
            id="workspace-sidebar-name-input"
            data-dialog-autofocus
            value={value}
            placeholder={title === '新建文件夹' ? '文件夹名称' : undefined}
            aria-describedby={suffix ? 'workspace-rename-format-hint' : undefined}
            onChange={(event) => onChange(event.target.value)}
            onFocus={(event) => { if (title === '重命名') event.currentTarget.select(); }}
            onKeyDown={(event) => event.key === 'Enter' && normalizedValue && onConfirm()}
            className="h-10 min-w-0 flex-1 rounded-lg border-0 bg-transparent px-3 text-[13px] font-normal text-claude-text outline-none"
          />
          {suffix && <span className="shrink-0 pr-3 text-[13px] font-normal text-claude-muted" aria-hidden="true">{suffix}</span>}
        </span>
      </div>
      {suffix && <p id="workspace-rename-format-hint" className="mt-1.5 text-[11px] leading-4 text-claude-muted">文件格式 {suffix} 将保持不变</p>}
      <div className="mt-4 flex justify-end gap-2">
        <button type="button" disabled={busy} onClick={onClose} className="h-10 rounded-lg border border-claude-border px-3.5 text-[12px] font-medium text-claude-secondary hover:bg-claude-hover disabled:opacity-40">取消</button>
        <button type="button" disabled={busy || !normalizedValue} onClick={onConfirm} className="h-10 rounded-lg bg-claude-accent px-3.5 text-[12px] font-semibold text-white shadow-[0_1px_2px_rgba(30,26,20,0.08)] disabled:opacity-40">确定</button>
      </div>
    </SidebarDialogShell>
  );
}

function SidebarDialogShell({ label, busy, returnFocusRef, onClose, children, panelClassName = 'max-w-md' }: { label: string; busy: boolean; returnFocusRef: React.RefObject<HTMLElement>; onClose: () => void; children: React.ReactNode; panelClassName?: string }) {
  const dialogRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const previousFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const fallbackReturnFocus = returnFocusRef.current;
    const frame = requestAnimationFrame(() => (dialogRef.current?.querySelector<HTMLElement>('[data-dialog-autofocus]') || dialogRef.current)?.focus());
    return () => {
      cancelAnimationFrame(frame);
      const returnTarget = previousFocus && previousFocus !== document.body && previousFocus.isConnected
        ? previousFocus
        : fallbackReturnFocus;
      returnTarget?.focus();
    };
  }, [returnFocusRef]);
  const handleKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    if (event.key === 'Escape' && !busy) { event.stopPropagation(); onClose(); return; }
    if (event.key !== 'Tab') return;
    const focusable = Array.from(dialogRef.current?.querySelectorAll<HTMLElement>('button:not([disabled]),input:not([disabled]),[tabindex]:not([tabindex="-1"])') || []);
    if (focusable.length === 0) return;
    if (event.shiftKey && document.activeElement === focusable[0]) { event.preventDefault(); focusable[focusable.length - 1].focus(); }
    else if (!event.shiftKey && document.activeElement === focusable[focusable.length - 1]) { event.preventDefault(); focusable[0].focus(); }
  };
  return <div className="fixed inset-0 z-[160] flex items-center justify-center bg-black/25 p-4" onMouseDown={(event) => event.target === event.currentTarget && !busy && onClose()}><div ref={dialogRef} role="dialog" aria-modal="true" aria-label={label} tabIndex={-1} onKeyDown={handleKeyDown} className={`w-full ${panelClassName} rounded-xl border border-claude-border bg-white p-4 shadow-[0_16px_40px_rgba(30,26,20,0.16)] outline-none focus-visible:ring-2 focus-visible:ring-claude-accent/40`}>{children}</div></div>;
}
