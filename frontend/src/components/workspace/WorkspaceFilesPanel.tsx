import { forwardRef, useCallback, useEffect, useImperativeHandle, useLayoutEffect, useRef, useState } from 'react';
import { FolderTree, X } from 'lucide-react';

import type { WorkspaceEntry } from '../../types/workspace';
import type { PendingFileDraftInfo } from '../../types';
import { rememberWorkspaceEntry, subscribeWorkspaceMutation } from '../../services/workspaceEvents';
import { WorkspaceApiError, workspaceApi, workspaceEntryToFileInfo } from '../../services/workspaceApi';
import { getFileIcon, getFileIconClass } from '../../utils/fileUtils';
import { FilePreview, type FilePreviewHandle, type FilePreviewSaveOptions } from '../FilePreview';
import { SessionFilesExpandButton } from '../session-files/SessionFilesControls';

const WORKSPACE_OWNER = { scope: 'workspace' as const, id: 'persistent', epoch: 0 };
const PREVIEW_OWNER = { ownerSessionId: 'workspace:persistent', ownerEpoch: 0 };
export interface WorkspaceFilesPanelHandle {
  owner: typeof WORKSPACE_OWNER;
  hasDirty: () => boolean;
  pendingFileDrafts: () => PendingFileDraftInfo[];
  saveDirty: (options?: FilePreviewSaveOptions) => Promise<{ ok: boolean; failedEntryIds: string[] }>;
  saveEntries: (
    entryIds: string[],
    options?: FilePreviewSaveOptions,
  ) => Promise<{ ok: boolean; failedEntryIds: string[] }>;
}

export const WorkspaceFilesPanel = forwardRef<WorkspaceFilesPanelHandle, {
  target: WorkspaceEntry | null;
  resolvingTarget?: boolean;
  isOpen: boolean;
  isExpanded: boolean;
  showExpandToggle?: boolean;
  onToggleExpanded: () => void;
  onActivateEntry?: (entry: WorkspaceEntry, options?: { replace?: boolean }) => void;
  onClose: () => void;
}>(function WorkspaceFilesPanel({
  target,
  resolvingTarget = false,
  isOpen,
  isExpanded,
  showExpandToggle = true,
  onToggleExpanded,
  onActivateEntry,
  onClose,
}, ref) {
  const [openFiles, setOpenFiles] = useState<WorkspaceEntry[]>([]);
  const [activeId, setActiveId] = useState<string | null>(null);
  const previewRefs = useRef(new Map<string, FilePreviewHandle>());
  const openFilesRef = useRef(openFiles);
  const tabButtonRefs = useRef(new Map<string, HTMLButtonElement>());
  const metadataRequestIdsRef = useRef(new Map<string, number>());
  openFilesRef.current = openFiles;

  const saveMatchingDirty = useCallback(async (
    matches: (entryId: string) => boolean,
    options?: FilePreviewSaveOptions,
  ) => {
    // 必须在第一个 await 前快照 ref，并同步调用全部 saveDirty 捕获编辑器当前内容。
    const dirtyHandles = Array.from(previewRefs.current.entries()).filter(([entryId, handle]) => (
      matches(entryId) && handle.isDirty(PREVIEW_OWNER)
    ));
    const saves = dirtyHandles.map(([entryId, handle]) => {
      try {
        return { entryId, promise: handle.saveDirty(PREVIEW_OWNER, options) };
      } catch (error) {
        return { entryId, promise: Promise.reject(error) };
      }
    });
    const settled = await Promise.allSettled(saves.map(({ promise }) => promise));
    const failedEntryIds = settled.flatMap((result, index) => (
      result.status === 'rejected' || !result.value.ok || result.value.stale
        ? [saves[index].entryId]
        : []
    ));
    const savedEntryIds = settled.flatMap((result, index) => (
      result.status === 'fulfilled' && result.value.ok && !result.value.stale
        ? [saves[index].entryId]
        : []
    ));
    return { ok: failedEntryIds.length === 0, failedEntryIds, savedEntryIds };
  }, []);

  useImperativeHandle(ref, () => ({
    owner: WORKSPACE_OWNER,
    hasDirty: () => Array.from(previewRefs.current.values()).some((handle) => handle.isDirty(PREVIEW_OWNER)),
    pendingFileDrafts: () => openFilesRef.current.flatMap((entry) => (
      previewRefs.current.get(entry.entry_id)?.isDirty(PREVIEW_OWNER)
        ? [{ source: 'workspace' as const, path: entry.path }]
        : []
    )),
    saveDirty: async (options) => {
      const { ok, failedEntryIds } = await saveMatchingDirty(() => true, options);
      return { ok, failedEntryIds };
    },
    saveEntries: async (entryIds, options) => {
      const selected = new Set(entryIds);
      const { ok, failedEntryIds } = await saveMatchingDirty(
        (entryId) => selected.has(entryId),
        options,
      );
      return { ok, failedEntryIds };
    },
  }), [saveMatchingDirty]);

  useLayoutEffect(() => {
    if (!target || target.kind !== 'file' || target.status !== 'active') return;
    rememberWorkspaceEntry(target);
    setOpenFiles((current) => {
      const existing = current.findIndex((entry) => entry.entry_id === target.entry_id);
      return existing < 0
        ? [...current, target]
        : current.map((entry, index) => index === existing ? target : entry);
    });
    setActiveId(target.entry_id);
  }, [target]);

  const acceptExternalEntry = useCallback((entry: WorkspaceEntry) => {
    const currentEntry = openFilesRef.current.find((item) => item.entry_id === entry.entry_id);
    if (currentEntry && currentEntry.revision >= entry.revision) return;
    rememberWorkspaceEntry(entry);
    const sameContentVersion = Boolean(
      currentEntry?.current_version_id
      && currentEntry.current_version_id === entry.current_version_id,
    );
    if (currentEntry && entry.status === 'active' && sameContentVersion) {
      // 重命名和移动只改路径元数据；即使编辑器里有草稿，也不应该伪装成内容冲突。
      setOpenFiles((current) => current.map((item) => item.entry_id === entry.entry_id ? entry : item));
      return;
    }
    const openPreview = previewRefs.current.get(entry.entry_id);
    if (openPreview?.isDirty(PREVIEW_OWNER)) {
      return;
    }
    setOpenFiles((current) => {
      const currentEntry = current.find((item) => item.entry_id === entry.entry_id);
      if (!currentEntry || currentEntry.revision >= entry.revision) return current;
      return entry.status === 'active'
        ? current.map((item) => item.entry_id === entry.entry_id ? entry : item)
        : current.filter((item) => item.entry_id !== entry.entry_id);
    });
    if (entry.status !== 'active') {
      setActiveId((current) => current === entry.entry_id ? null : current);
    }
  }, []);

  const refreshExternalEntry = useCallback(async (entryId: string) => {
    const requestId = (metadataRequestIdsRef.current.get(entryId) || 0) + 1;
    metadataRequestIdsRef.current.set(entryId, requestId);
    try {
      const entry = await workspaceApi.getEntry(entryId);
      if (metadataRequestIdsRef.current.get(entryId) !== requestId) return;
      acceptExternalEntry(entry);
    } catch (error) {
      if (metadataRequestIdsRef.current.get(entryId) !== requestId) return;
      const missing = error instanceof WorkspaceApiError && error.status === 404;
      const dirty = previewRefs.current.get(entryId)?.isDirty(PREVIEW_OWNER) || false;
      if (missing && !dirty) {
        setOpenFiles((current) => current.filter((entry) => entry.entry_id !== entryId));
        setActiveId((current) => current === entryId ? null : current);
        return;
      }
      console.error('Failed to refresh workspace entry in background:', error);
    }
  }, [acceptExternalEntry]);

  const removeExternalEntries = useCallback((entryIds: string[]) => {
    const removed = new Set(entryIds);
    removed.forEach((entryId) => {
      metadataRequestIdsRef.current.set(
        entryId,
        (metadataRequestIdsRef.current.get(entryId) || 0) + 1,
      );
    });
    setOpenFiles((current) => {
      const activeIndex = current.findIndex((entry) => entry.entry_id === activeId);
      const next = current.filter((entry) => !removed.has(entry.entry_id));
      if (next.length === current.length) return current;
      setActiveId((active) => active && removed.has(active)
        ? next[Math.min(Math.max(activeIndex, 0), next.length - 1)]?.entry_id || null
        : active);
      return next;
    });
  }, [activeId]);

  useEffect(() => subscribeWorkspaceMutation((detail) => {
    if (detail.origin === 'workspace-editor') return;
    if (detail.tombstone && detail.affectedEntryIds?.length) {
      removeExternalEntries(detail.affectedEntryIds);
      return;
    }
    if (detail.tombstone && detail.entryId) {
      removeExternalEntries([detail.entryId]);
      return;
    }
    if (detail.entry) {
      acceptExternalEntry(detail.entry);
      return;
    }
    if (detail.entryId) void refreshExternalEntry(detail.entryId);
  }), [acceptExternalEntry, refreshExternalEntry, removeExternalEntries]);

  const activateEntry = (entry: WorkspaceEntry, replace = false) => {
    setActiveId(entry.entry_id);
    onActivateEntry?.(entry, { replace });
  };

  const finalizeCloseTab = (entryId: string) => {
    const current = openFilesRef.current;
    const index = current.findIndex((entry) => entry.entry_id === entryId);
    const next = current.filter((entry) => entry.entry_id !== entryId);
    openFilesRef.current = next;
    setOpenFiles(next);
    if (next.length === 0) {
      setActiveId(null);
      onClose();
    } else if (activeId === entryId) {
      const nextEntry = next[Math.min(Math.max(index, 0), next.length - 1)];
      activateEntry(nextEntry, true);
      window.requestAnimationFrame(() => tabButtonRefs.current.get(nextEntry.entry_id)?.focus());
    }
  };

  const requestCloseTab = (entryId: string) => {
    const handle = previewRefs.current.get(entryId);
    if (handle?.isDirty(PREVIEW_OWNER)) {
      void handle.saveDirty(PREVIEW_OWNER).catch((error) => {
        console.error('Failed to sync workspace draft in background:', error);
      });
    }
    finalizeCloseTab(entryId);
  };

  if (!isOpen) return null;
  const activeFile = openFiles.find((entry) => entry.entry_id === activeId) || null;
  return (
    <div className="relative h-full min-h-0 w-full min-w-0 bg-white" data-testid="workspace-files-panel">
      <div className={resolvingTarget ? 'hidden' : 'flex h-full min-h-0 flex-col'} aria-hidden={resolvingTarget || undefined}>
      <div className="flex h-14 shrink-0 items-center gap-1 border-b border-claude-border bg-white px-3">
        <span className="mr-1 flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-claude-accent/10 text-claude-accent" title="工作区文件"><FolderTree size={16} /></span>
        <div role="tablist" aria-label="已打开的工作区文件" className="flex min-w-0 flex-1 items-center gap-1 overflow-x-auto">
          {openFiles.map((entry) => {
            const fileInfo = workspaceEntryToFileInfo(entry);
            const Icon = getFileIcon(fileInfo);
            const active = entry.entry_id === activeId;
            return (
              <div key={entry.entry_id} className={`flex h-8 max-w-[220px] shrink-0 items-center rounded-md border ${active ? 'border-claude-border bg-white text-claude-text shadow-sm' : 'border-transparent bg-claude-surface/65 text-claude-muted hover:bg-claude-hover'}`}>
                <button ref={(button) => { if (button) tabButtonRefs.current.set(entry.entry_id, button); else tabButtonRefs.current.delete(entry.entry_id); }} type="button" role="tab" aria-selected={active} title={entry.name} onClick={() => activateEntry(entry)} className="flex min-w-0 flex-1 items-center gap-1.5 py-1.5 pl-2.5 text-xs focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-claude-accent/45"><Icon size={13} className={`shrink-0 ${getFileIconClass(fileInfo)}`} aria-hidden="true" /><span className="min-w-0 flex-1 truncate">{entry.name}</span></button>
                <button type="button" onClick={() => requestCloseTab(entry.entry_id)} className="mr-0.5 inline-flex h-8 w-8 items-center justify-center rounded text-claude-muted hover:bg-claude-hover" aria-label={`关闭 ${entry.name}`}><X size={13} aria-hidden="true" /></button>
              </div>
            );
          })}
        </div>
        {showExpandToggle && <SessionFilesExpandButton expanded={isExpanded} onToggle={onToggleExpanded} />}
        <button type="button" onClick={onClose} className="inline-flex h-8 w-8 items-center justify-center rounded-md text-claude-muted hover:bg-claude-hover focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-claude-accent/45" aria-label="收起工作区文件" title="收起工作区文件"><X size={15} /></button>
      </div>
      <div className="min-h-0 flex-1">
        {activeFile ? openFiles.map((entry) => {
          const info = workspaceEntryToFileInfo(entry);
          return (
            <div key={entry.entry_id} className={entry.entry_id === activeId ? 'h-full min-h-0' : 'hidden'} aria-hidden={entry.entry_id !== activeId}>
              <FilePreview
                ref={(handle) => {
                  if (handle) previewRefs.current.set(entry.entry_id, handle);
                  else previewRefs.current.delete(entry.entry_id);
                }}
                inline
                file={info}
                sessionId={PREVIEW_OWNER.ownerSessionId}
                ownerEpoch={PREVIEW_OWNER.ownerEpoch}
                refreshInPlace
                onClose={() => requestCloseTab(entry.entry_id)}
                previewUrlBuilder={(resolved) => resolved.path === entry.path ? workspaceApi.previewContentUrl(entry.entry_id, entry.current_version_id) : workspaceApi.contentPathUrl(resolved.path)}
                onDownloadFile={async () => workspaceApi.download(entry)}
              />
            </div>
          );
        }) : (
          <div className="flex h-full items-center justify-center text-sm text-claude-muted">从左侧工作区选择文件</div>
        )}
      </div>
      </div>
      {resolvingTarget && (
        <div className="absolute inset-0 flex items-center justify-center bg-white text-sm text-claude-muted" data-testid="workspace-target-loading" role="status" aria-live="polite">
          正在打开工作区文件…
        </div>
      )}
    </div>
  );
});
