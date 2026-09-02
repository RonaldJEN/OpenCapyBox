import type { WorkspaceEntry } from '../types/workspace';
import { invalidateWorkspacePreviewCache } from './filePreviewCache';

export interface WorkspaceMutationEvent {
  entry?: WorkspaceEntry;
  entryId?: string;
  path?: string;
  parentId?: string | null;
  operation: string;
  revision?: number;
  versionId?: string | null;
  tombstone?: boolean;
  affectedEntryIds?: string[];
  origin?: 'workspace-editor' | 'server' | 'local';
}

export function resetWorkspaceEventsForTests(): void {
  deletedEntries.clear();
  rememberedEntries.clear();
  latestInvalidationRevision.clear();
  latestInvalidationIdentity.clear();
  notifyProjectionChanged();
}

const mutationTarget = new EventTarget();
const deletedEntries = new Set<string>();
const rememberedEntries = new Map<string, WorkspaceEntry>();
const latestInvalidationRevision = new Map<string, number>();
const latestInvalidationIdentity = new Map<string, string>();
let projectionRevision = 0;
const projectionListeners = new Set<() => void>();

function notifyProjectionChanged(): void {
  projectionRevision += 1;
  projectionListeners.forEach((listener) => listener());
}

type WorkspaceInvalidation = Omit<WorkspaceMutationEvent, 'entry'>;

export interface WorkspaceChangeInvalidationInput {
  entry_id: string;
  operation?: string;
  path?: string;
  revision?: number | string | null;
  version_id?: string | null;
  current_version_id?: string | null;
  affected_entry_ids?: string[];
}

function invalidationIdentity(detail: WorkspaceInvalidation): string {
  if (detail.revision !== undefined) return `revision:${detail.revision}`;
  if (detail.versionId) return `version:${detail.versionId}`;
  return '';
}

function dispatchWorkspaceInvalidation(detail: WorkspaceInvalidation): void {
  if (detail.tombstone) {
    emitWorkspaceMutation(detail);
    return;
  }
  if (detail.entryId && deletedEntries.has(detail.entryId)) return;
  if (detail.entryId) {
    const rememberedRevision = rememberedEntries.get(detail.entryId)?.revision;
    const latestRevision = Math.max(
      latestInvalidationRevision.get(detail.entryId) ?? -1,
      rememberedRevision ?? -1,
    );
    if (detail.revision !== undefined && detail.revision <= latestRevision) return;
    const identity = invalidationIdentity(detail);
    if (identity && latestInvalidationIdentity.get(detail.entryId) === identity) return;
    if (detail.revision !== undefined) latestInvalidationRevision.set(detail.entryId, detail.revision);
    if (identity) latestInvalidationIdentity.set(detail.entryId, identity);
  }
  notifyProjectionChanged();
  mutationTarget.dispatchEvent(new CustomEvent<WorkspaceMutationEvent>('mutation', { detail }));
}

export function emitWorkspaceInvalidation(detail: WorkspaceInvalidation): void {
  dispatchWorkspaceInvalidation(detail);
}

export function emitWorkspaceInvalidations(details: WorkspaceInvalidation[]): void {
  const latestByEntry = new Map<string, WorkspaceInvalidation>();
  const withoutEntry: WorkspaceInvalidation[] = [];
  details.forEach((detail) => {
    if (!detail.entryId) {
      withoutEntry.push(detail);
      return;
    }
    const previous = latestByEntry.get(detail.entryId);
    if (
      !previous
      || (detail.revision !== undefined && (
        previous.revision === undefined || detail.revision >= previous.revision
      ))
    ) latestByEntry.set(detail.entryId, detail);
  });
  [...withoutEntry, ...latestByEntry.values()].forEach(dispatchWorkspaceInvalidation);
}

export function emitWorkspaceChangeInvalidations(
  changes: WorkspaceChangeInvalidationInput[],
): void {
  emitWorkspaceInvalidations(changes.map((change) => {
    const parsedRevision = change.revision === null || change.revision === undefined
      ? Number.NaN
      : Number(change.revision);
    return {
      operation: change.operation || 'updated',
      tombstone: change.operation?.toUpperCase() === 'DELETED',
      affectedEntryIds: change.affected_entry_ids,
      entryId: change.entry_id,
      path: change.path,
      revision: Number.isFinite(parsedRevision) ? parsedRevision : undefined,
      versionId: change.current_version_id || change.version_id || null,
      origin: 'server' as const,
    };
  }));
}

export function rememberWorkspaceEntry(entry: WorkspaceEntry): void {
  if (deletedEntries.has(entry.entry_id)) return;
  const current = rememberedEntries.get(entry.entry_id);
  if (current && current.revision > entry.revision) return;
  rememberedEntries.set(entry.entry_id, entry);
  const latestRevision = latestInvalidationRevision.get(entry.entry_id) ?? -1;
  if (entry.revision >= latestRevision) {
    latestInvalidationRevision.set(entry.entry_id, entry.revision);
    latestInvalidationIdentity.set(entry.entry_id, `revision:${entry.revision}`);
  }
}

export function emitWorkspaceMutation(detail: WorkspaceMutationEvent): void {
  if (detail.tombstone) {
    const affectedEntryIds = detail.affectedEntryIds || (detail.entryId ? [detail.entryId] : []);
    for (const entryId of affectedEntryIds) {
      deletedEntries.add(entryId);
      rememberedEntries.delete(entryId);
    }
    invalidateWorkspacePreviewCache(affectedEntryIds);
  } else if (deletedEntries.has(detail.entry?.entry_id || detail.entryId || '')) {
    return;
  }
  if (detail.entry) rememberWorkspaceEntry(detail.entry);
  notifyProjectionChanged();
  mutationTarget.dispatchEvent(new CustomEvent<WorkspaceMutationEvent>('mutation', { detail }));
}

export function isWorkspaceEntryDeleted(entryId: string | null | undefined): boolean {
  return Boolean(entryId && deletedEntries.has(entryId));
}

export function getWorkspaceProjectionRevision(): number {
  return projectionRevision;
}

export function subscribeWorkspaceProjection(listener: () => void): () => void {
  projectionListeners.add(listener);
  return () => projectionListeners.delete(listener);
}

export function getRememberedWorkspaceEntry(entryId: string): WorkspaceEntry | undefined {
  return rememberedEntries.get(entryId);
}

export function subscribeWorkspaceMutation(
  listener: (detail: WorkspaceMutationEvent) => void,
): () => void {
  const handler = (event: Event) => {
    listener((event as CustomEvent<WorkspaceMutationEvent>).detail);
  };
  mutationTarget.addEventListener('mutation', handler);
  return () => mutationTarget.removeEventListener('mutation', handler);
}

export interface WorkspaceNavigationEvent {
  entryId?: string;
}

export function requestOpenWorkspace(entryId?: string): void {
  window.dispatchEvent(new CustomEvent<WorkspaceNavigationEvent>('workspace:navigate', {
    detail: { entryId },
  }));
}
