import type { FileInfo } from '../types';
import type { WorkspaceEntry } from '../types/workspace';
import { emitWorkspaceMutation, subscribeWorkspaceMutation } from './workspaceEvents';
import {
  createWorkspaceIdempotencyKey,
  WorkspaceApiError,
  workspaceApi,
  workspaceEntryToFileInfo,
} from './workspaceApi';

type WorkspaceDraftKind = 'markdown' | 'spreadsheet';
type WorkspaceCheckpointKind = 'web_idle' | 'web_close' | 'web_periodic';

export interface WorkspaceDraftRecord {
  key: string;
  entryId: string;
  kind: WorkspaceDraftKind;
  file: FileInfo;
  generation: number;
  idempotencyKey: string;
  content: string | ArrayBuffer;
  status: 'pending' | 'saving' | 'retry';
  retryCount: number;
  updatedAt: number;
}

export interface WorkspaceDraftLossNotice {
  entryId: string;
  path: string;
  name: string;
  failedAt: number;
  lastSavedAt: string;
  message: string;
}

const RETRY_BASE_DELAY_MS = 1800;
const RETRY_MAX_DELAY_MS = 30_000;
const CHECKPOINT_IDLE_DELAY_MS = 30_000;
const CHECKPOINT_MAX_INTERVAL_MS = 5 * 60_000;
const deletedEntryIds = new Set<string>();
let unsubscribeMutations: (() => void) | null = null;
const records = new Map<string, WorkspaceDraftRecord>();
const inFlight = new Map<string, Promise<FileInfo>>();
const retryTimers = new Map<string, number>();
const checkpointTimers = new Map<string, number>();
const checkpointWindowStartedAt = new Map<string, number>();
const lossNotices = new Map<string, WorkspaceDraftLossNotice>();
const lossListeners = new Set<(notice: WorkspaceDraftLossNotice) => void>();

export function workspaceDraftKey(entryId: string): string {
  return `workspace::${entryId}`;
}

function cloneContent(content: string | ArrayBuffer): string | ArrayBuffer {
  return typeof content === 'string' ? content : content.slice(0);
}

function clearRetryTimer(key: string): void {
  const timer = retryTimers.get(key);
  if (timer !== undefined && typeof window !== 'undefined') window.clearTimeout(timer);
  retryTimers.delete(key);
}

function scheduleRetry(key: string, retryCount: number): void {
  if (retryTimers.has(key) || typeof window === 'undefined') return;
  const delay = Math.min(
    RETRY_BASE_DELAY_MS * (2 ** Math.max(0, retryCount - 1)),
    RETRY_MAX_DELAY_MS,
  );
  const timer = window.setTimeout(() => {
    retryTimers.delete(key);
    void flushWorkspaceDraft(key).catch(() => undefined);
  }, delay);
  retryTimers.set(key, timer);
}

function isRetryableWorkspaceFailure(error: unknown): boolean {
  if (error instanceof WorkspaceApiError) {
    if (error.detail.mutation_state === 'failed' || error.detail.outcome === 'unknown') {
      return false;
    }
    if (
      error.detail.code === 'MUTATION_IN_PROGRESS'
      || error.detail.code === 'WORKSPACE_MUTATION_IN_PROGRESS'
    ) return true;
    return error.status === undefined || error.status >= 500;
  }
  const status = typeof (error as { status?: unknown })?.status === 'number'
    ? Number((error as { status: number }).status)
    : 0;
  return status === 0 || status >= 500;
}

function publishDraftLoss(record: WorkspaceDraftRecord): void {
  const notice: WorkspaceDraftLossNotice = {
    entryId: record.entryId,
    path: record.file.workspace_path || record.file.path,
    name: record.file.name,
    failedAt: Date.now(),
    lastSavedAt: record.file.modified,
    message: '刚才的修改未保存，已恢复到最近保存版本。',
  };
  lossNotices.set(record.entryId, notice);
  lossListeners.forEach((listener) => {
    try {
      listener(notice);
    } catch (error) {
      console.error('Workspace draft loss listener failed:', error);
    }
  });
}

export function subscribeWorkspaceDraftLosses(
  listener: (notice: WorkspaceDraftLossNotice) => void,
): () => void {
  lossListeners.add(listener);
  return () => lossListeners.delete(listener);
}

export function takeWorkspaceDraftLossNotice(
  entryId: string,
): WorkspaceDraftLossNotice | null {
  const notice = lossNotices.get(entryId) || null;
  if (notice) lossNotices.delete(entryId);
  return notice;
}

function clearCheckpointTimer(entryId: string): void {
  const timer = checkpointTimers.get(entryId);
  if (timer !== undefined && typeof window !== 'undefined') window.clearTimeout(timer);
  checkpointTimers.delete(entryId);
}

export async function checkpointWorkspaceFile(
  file: FileInfo,
  checkpointKind: WorkspaceCheckpointKind = 'web_close',
): Promise<void> {
  if (!file.entry_id || !file.version_id || !file.revision) return;
  clearCheckpointTimer(file.entry_id);
  try {
    await workspaceApi.checkpoint(
      file.entry_id,
      Number(file.revision),
      file.version_id,
      checkpointKind,
    );
    checkpointWindowStartedAt.delete(file.entry_id);
  } catch (error) {
    const status = typeof (error as { status?: unknown })?.status === 'number'
      ? Number((error as { status: number }).status)
      : 0;
    if (status === 404 || status === 409) {
      checkpointWindowStartedAt.delete(file.entry_id);
    } else {
      scheduleWorkspaceCheckpoint(file, RETRY_BASE_DELAY_MS);
    }
    throw error;
  }
}

function scheduleWorkspaceCheckpoint(file: FileInfo, retryDelay?: number): void {
  if (!file.entry_id || !file.version_id || !file.revision || typeof window === 'undefined') return;
  const entryId = file.entry_id;
  clearCheckpointTimer(entryId);
  const now = Date.now();
  const startedAt = checkpointWindowStartedAt.get(entryId) ?? now;
  checkpointWindowStartedAt.set(entryId, startedAt);
  const untilPeriodic = Math.max(0, startedAt + CHECKPOINT_MAX_INTERVAL_MS - now);
  const delay = retryDelay ?? Math.min(CHECKPOINT_IDLE_DELAY_MS, untilPeriodic);
  const checkpointKind: WorkspaceCheckpointKind = untilPeriodic <= CHECKPOINT_IDLE_DELAY_MS
    ? 'web_periodic'
    : 'web_idle';
  const timer = window.setTimeout(() => {
    checkpointTimers.delete(entryId);
    void checkpointWorkspaceFile(file, checkpointKind).catch(() => undefined);
  }, delay);
  checkpointTimers.set(entryId, timer);
}

function queueWorkspaceDraft(
  file: FileInfo,
  kind: WorkspaceDraftKind,
  content: string | ArrayBuffer,
): WorkspaceDraftRecord {
  if (!file.entry_id) throw new Error('工作区草稿缺少文件标识');
  if (deletedEntryIds.has(file.entry_id)) throw new Error('工作区文件已删除');
  clearCheckpointTimer(file.entry_id);
  if (!checkpointWindowStartedAt.has(file.entry_id)) {
    checkpointWindowStartedAt.set(file.entry_id, Date.now());
  }
  const key = workspaceDraftKey(file.entry_id);
  const previous = records.get(key);
  const generation = (previous?.generation || 0) + 1;
  const record: WorkspaceDraftRecord = {
    key,
    entryId: file.entry_id,
    kind,
    file: { ...file },
    generation,
    idempotencyKey: createWorkspaceIdempotencyKey(`workspace-draft-${generation}`),
    content: cloneContent(content),
    status: 'pending',
    retryCount: 0,
    updatedAt: Date.now(),
  };
  clearRetryTimer(key);
  records.set(key, record);
  return record;
}

export function queueWorkspaceMarkdownDraft(file: FileInfo, content: string): WorkspaceDraftRecord {
  return queueWorkspaceDraft(file, 'markdown', content);
}

export function queueWorkspaceSpreadsheetDraft(file: FileInfo, content: ArrayBuffer): WorkspaceDraftRecord {
  return queueWorkspaceDraft(file, 'spreadsheet', content);
}

export async function getWorkspaceDraft(entryId: string): Promise<WorkspaceDraftRecord | null> {
  const record = records.get(workspaceDraftKey(entryId));
  return record ? { ...record, content: cloneContent(record.content) } : null;
}

function draftEntry(record: WorkspaceDraftRecord): WorkspaceEntry {
  return {
    entry_id: record.entryId,
    parent_id: null,
    name: record.file.name,
    kind: 'file',
    path: record.file.workspace_path || record.file.path,
    size_bytes: record.file.size,
    mime_type: null,
    sha256: null,
    revision: Number(record.file.revision || 0),
    current_version_id: record.file.version_id || null,
    tree_revision: record.file.tree_revision,
    status: 'active',
    created_at: record.file.modified,
    updated_at: record.file.modified,
  };
}

export async function flushWorkspaceDraft(key: string): Promise<FileInfo> {
  const existing = inFlight.get(key);
  if (existing) return existing;
  const task = (async () => {
    while (true) {
      const record = records.get(key);
      if (!record) throw new Error('待保存工作区草稿不存在');
      const generation = record.generation;
      record.status = 'saving';
      records.set(key, record);
      try {
        const result = await workspaceApi.updateContent(
          draftEntry(record),
          record.content,
          record.kind === 'markdown'
            ? 'text/markdown; charset=utf-8'
            : record.file.name.toLowerCase().endsWith('.csv')
              ? 'text/csv; charset=utf-8'
              : 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
          record.idempotencyKey,
        );
        if (deletedEntryIds.has(record.entryId)) throw new Error('工作区文件已删除');
        const updated: FileInfo = {
          ...workspaceEntryToFileInfo(result.entry),
          workspace_auto_merged: Boolean(result.auto_merged),
        };
        scheduleWorkspaceCheckpoint(updated);
        window.setTimeout(() => emitWorkspaceMutation({
          operation: 'update_content',
          entry: result.entry,
          parentId: result.entry.parent_id,
          revision: result.entry.revision,
          versionId: result.entry.current_version_id,
          origin: 'local',
        }), 0);
        const latest = records.get(key);
        if (!latest || latest.generation === generation) {
          records.delete(key);
          clearRetryTimer(key);
          return { ...updated, outbox_generation: generation };
        }
        if (!result.auto_merged) {
          latest.file = {
            ...updated,
            workspace_auto_merged: undefined,
            outbox_generation: undefined,
          };
        }
        latest.status = 'pending';
        latest.retryCount = 0;
        records.set(key, latest);
      } catch (error) {
        const latest = records.get(key);
        if (latest) {
          if (isRetryableWorkspaceFailure(error)) {
            latest.status = 'retry';
            latest.retryCount = Number(latest.retryCount || 0) + 1;
            records.set(key, latest);
            scheduleRetry(key, latest.retryCount);
          } else if (latest.generation === generation) {
            records.delete(key);
            clearRetryTimer(key);
            clearCheckpointTimer(latest.entryId);
            checkpointWindowStartedAt.delete(latest.entryId);
            publishDraftLoss(latest);
          } else {
            latest.status = 'pending';
            latest.retryCount = 0;
            records.set(key, latest);
            scheduleRetry(key, 1);
          }
        }
        throw error;
      }
    }
  })();
  inFlight.set(key, task);
  const clearInFlight = () => {
    if (inFlight.get(key) === task) inFlight.delete(key);
  };
  void task.then(clearInFlight, clearInFlight);
  return task;
}

export function flushWorkspaceDraftInBackground(key: string): void {
  void flushWorkspaceDraft(key).catch(() => undefined);
}

export async function startWorkspaceDraftOutbox(): Promise<void> {
  if (!unsubscribeMutations) {
    unsubscribeMutations = subscribeWorkspaceMutation((detail) => {
      if (!detail.tombstone) return;
      void discardWorkspaceDrafts(
        detail.affectedEntryIds || (detail.entryId ? [detail.entryId] : []),
      );
    });
  }
}

export async function discardWorkspaceDrafts(entryIds: string[]): Promise<void> {
  entryIds.forEach((entryId) => {
    deletedEntryIds.add(entryId);
    const key = workspaceDraftKey(entryId);
    records.delete(key);
    clearRetryTimer(key);
    clearCheckpointTimer(entryId);
    checkpointWindowStartedAt.delete(entryId);
    lossNotices.delete(entryId);
  });
}

export function resetWorkspaceDraftOutboxForTests(): void {
  deletedEntryIds.clear();
  unsubscribeMutations?.();
  unsubscribeMutations = null;
  retryTimers.forEach((timer) => window.clearTimeout(timer));
  retryTimers.clear();
  checkpointTimers.forEach((timer) => window.clearTimeout(timer));
  checkpointTimers.clear();
  checkpointWindowStartedAt.clear();
  lossNotices.clear();
  lossListeners.clear();
  records.clear();
  inFlight.clear();
}
