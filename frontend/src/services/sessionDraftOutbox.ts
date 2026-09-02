import type { FileInfo } from '../types';
import { apiService } from './api';
import { buildSandboxFileUrl } from '../utils/fileUtils';

type DraftKind = 'markdown' | 'spreadsheet';

export interface SessionDraftRecord {
  key: string;
  sessionId: string;
  path: string;
  kind: DraftKind;
  file: FileInfo;
  generation: number;
  content: string | ArrayBuffer;
  status: 'pending' | 'saving' | 'retry' | 'conflict' | 'retained';
  updatedAt: number;
  saveId?: string;
  errorMessage?: string;
}

export interface SessionSaveSnapshot {
  key: string;
  file: FileInfo;
  content: string | ArrayBuffer;
  generation: number;
}

const DB_NAME = 'opencapybox-session-drafts';
const DB_VERSION = 1;
const STORE_NAME = 'drafts';
const RETRY_DELAY_MS = 1800;
const records = new Map<string, SessionDraftRecord>();
const inFlight = new Map<string, Promise<FileInfo>>();
const retryTimers = new Map<string, number>();
const generations = new Map<string, number>();
const savedSnapshots = new Map<string, SessionSaveSnapshot>();
const savedListeners = new Set<(snapshot: SessionSaveSnapshot) => void>();
let hydrationPromise: Promise<void> | null = null;
let persistenceTail: Promise<void> = Promise.resolve();

function normalizePath(path: string): string {
  return path.replace(/\\/g, '/').replace(/^\/+/, '');
}

export function sessionDraftKey(sessionId: string, path: string): string {
  return `${sessionId}::${normalizePath(path)}`;
}

function cloneContent(content: string | ArrayBuffer): string | ArrayBuffer {
  return typeof content === 'string' ? content : content.slice(0);
}

function sameContent(left: string | ArrayBuffer, right: string | ArrayBuffer): boolean {
  if (typeof left === 'string' || typeof right === 'string') return left === right;
  if (left.byteLength !== right.byteLength) return false;
  const a = new Uint8Array(left), b = new Uint8Array(right);
  return a.every((value, index) => value === b[index]);
}

export function subscribeSessionSaves(listener: (snapshot: SessionSaveSnapshot) => void): () => void {
  savedListeners.add(listener);
  return () => savedListeners.delete(listener);
}

export function getSessionSaveSnapshot(sessionId: string, path: string): SessionSaveSnapshot | undefined {
  return savedSnapshots.get(sessionDraftKey(sessionId, path));
}

export function hasPendingSessionDraft(sessionId: string, path: string): boolean {
  return records.has(sessionDraftKey(sessionId, path));
}

function publishSaved(snapshot: SessionSaveSnapshot): void {
  savedSnapshots.set(snapshot.key, snapshot);
  // Cache only recent acknowledgements; durable unsaved drafts live separately.
  if (savedSnapshots.size > 30) savedSnapshots.delete(savedSnapshots.keys().next().value!);
  savedListeners.forEach((listener) => {
    try { listener(snapshot); } catch (error) { console.error('Session save view update failed:', error); }
  });
}

function openDatabase(): Promise<IDBDatabase | null> {
  if (typeof indexedDB === 'undefined') return Promise.resolve(null);
  return new Promise((resolve) => {
    const request = indexedDB.open(DB_NAME, DB_VERSION);
    request.onupgradeneeded = () => {
      const database = request.result;
      if (!database.objectStoreNames.contains(STORE_NAME)) {
        database.createObjectStore(STORE_NAME, { keyPath: 'key' });
      }
    };
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => resolve(null);
  });
}

function enqueuePersistence(operation: () => Promise<void>): Promise<void> {
  const queued = persistenceTail.then(operation, operation);
  persistenceTail = queued.catch(() => undefined);
  return queued;
}

function persist(record: SessionDraftRecord): Promise<void> {
  const snapshot = { ...record, content: cloneContent(record.content) };
  return enqueuePersistence(async () => {
    const database = await openDatabase();
    if (!database) return;
    await new Promise<void>((resolve) => {
      const transaction = database.transaction(STORE_NAME, 'readwrite');
      transaction.objectStore(STORE_NAME).put(snapshot);
      transaction.oncomplete = () => resolve();
      transaction.onerror = () => resolve();
      transaction.onabort = () => resolve();
    });
    database.close();
  });
}

function removePersisted(key: string): Promise<void> {
  return enqueuePersistence(async () => {
    const database = await openDatabase();
    if (!database) return;
    await new Promise<void>((resolve) => {
      const transaction = database.transaction(STORE_NAME, 'readwrite');
      transaction.objectStore(STORE_NAME).delete(key);
      transaction.oncomplete = () => resolve();
      transaction.onerror = () => resolve();
      transaction.onabort = () => resolve();
    });
    database.close();
  });
}

async function hydrate(): Promise<void> {
  if (hydrationPromise) return hydrationPromise;
  hydrationPromise = (async () => {
    const database = await openDatabase();
    if (!database) return;
    const stored = await new Promise<SessionDraftRecord[]>((resolve) => {
      const transaction = database.transaction(STORE_NAME, 'readonly');
      const request = transaction.objectStore(STORE_NAME).getAll();
      request.onsuccess = () => resolve((request.result || []) as SessionDraftRecord[]);
      request.onerror = () => resolve([]);
    });
    database.close();
    stored.forEach((record) => {
      generations.set(record.key, Math.max(generations.get(record.key) || 0, record.generation));
      const current = records.get(record.key);
      if (!current || current.generation < record.generation) {
        const status = record.status === 'conflict' || record.status === 'retained'
          ? record.status
          : 'pending';
        records.set(record.key, { ...record, status, content: cloneContent(record.content) });
      }
    });
  })();
  return hydrationPromise;
}

function scheduleRetry(key: string) {
  if (retryTimers.has(key) || typeof window === 'undefined') return;
  const timer = window.setTimeout(() => {
    retryTimers.delete(key);
    void flushSessionDraft(key).catch(() => undefined);
  }, RETRY_DELAY_MS);
  retryTimers.set(key, timer);
}

function newSaveId(generation: number): string {
  return typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function'
    ? crypto.randomUUID()
    : `${Date.now()}-${generation}-${Math.random()}`;
}

function queueDraft(
  sessionId: string,
  file: FileInfo,
  kind: DraftKind,
  content: string | ArrayBuffer,
): SessionDraftRecord {
  const key = sessionDraftKey(sessionId, file.path);
  const previous = records.get(key);
  if (previous?.kind === kind && sameContent(previous.content, content)) return previous;
  const generation = Math.max(generations.get(key) || 0, previous?.generation || 0) + 1;
  generations.set(key, generation);
  const record: SessionDraftRecord = {
    key,
    sessionId,
    path: normalizePath(file.path),
    kind,
    file: { ...(previous?.file || file), session_id: file.session_id || sessionId },
    generation,
    saveId: newSaveId(generation),
    content: cloneContent(content),
    status: 'pending',
    updatedAt: Date.now(),
  };
  records.set(key, record);
  void persist(record);
  return record;
}

export function queueSessionMarkdownDraft(
  sessionId: string,
  file: FileInfo,
  content: string,
): SessionDraftRecord {
  return queueDraft(sessionId, file, 'markdown', content);
}

export function queueSessionSpreadsheetDraft(
  sessionId: string,
  file: FileInfo,
  content: ArrayBuffer,
): SessionDraftRecord {
  return queueDraft(sessionId, file, 'spreadsheet', content);
}

export async function getSessionDraft(
  sessionId: string,
  path: string,
): Promise<SessionDraftRecord | null> {
  await hydrate();
  const record = records.get(sessionDraftKey(sessionId, path));
  return record ? { ...record, content: cloneContent(record.content) } : null;
}

export async function resolveSessionDraftForRead(
  sessionId: string,
  file: FileInfo,
  content: string | ArrayBuffer,
): Promise<SessionDraftRecord | null> {
  await hydrate();
  const key = sessionDraftKey(sessionId, file.path);
  const draft = records.get(key);
  if (!draft) return null;
  // A newer GET may observe our own PUT before its acknowledgement arrives.
  // Only the outbox may settle that operation; reopening cannot call it a conflict.
  if (inFlight.has(key)) return { ...draft, content: cloneContent(draft.content) };
  if (sameContent(draft.content, content)) {
    records.delete(key);
    await removePersisted(key);
    publishSaved({ key, file, content, generation: draft.generation });
    return null;
  }
  if (!draft.file.edit_base_token && file.edit_base_token) {
    if (String(draft.file.revision) === String(file.revision)) {
      draft.file = { ...file };
      draft.status = 'pending';
      draft.errorMessage = undefined;
      await persist(draft);
    } else {
      draft.status = 'retained';
      draft.errorMessage = '旧草稿缺少可验证的编辑基线，已保留，未覆盖当前文件。';
      await persist(draft);
    }
  }
  return { ...draft, content: cloneContent(draft.content) };
}

export async function retainSessionDraft(
  sessionId: string,
  path: string,
): Promise<SessionDraftRecord | null> {
  await hydrate();
  const key = sessionDraftKey(sessionId, path);
  const record = records.get(key);
  if (!record) return null;
  record.status = 'retained';
  records.set(key, record);
  await persist(record);
  return { ...record, content: cloneContent(record.content) };
}

export async function flushSessionDraft(key: string): Promise<FileInfo> {
  const existing = inFlight.get(key);
  if (existing) return existing;
  const task = (async () => {
    while (true) {
      const record = records.get(key);
      if (!record) throw new Error('待保存草稿不存在');
      if (record.status === 'conflict' || record.status === 'retained') {
        throw new Error('未保存草稿基于旧文件版本，已保留且不会自动覆盖当前文件');
      }
      const generation = record.generation;
      record.saveId ||= newSaveId(generation);
      record.status = 'saving';
      records.set(key, record);
      void persist(record);
      try {
        const updated = record.kind === 'markdown'
          ? await apiService.updateSessionMarkdown(record.sessionId, record.file, record.content as string, record.saveId)
          : await apiService.updateSessionSpreadsheet(record.sessionId, record.file, record.content as ArrayBuffer, record.saveId);
        let savedContent = cloneContent(record.content);
        if (updated.session_auto_merged && updated.edit_base_token) {
          const url = buildSandboxFileUrl(record.sessionId, record.path, true);
          const response = await fetch(`${url}&edit=true&base_token=${encodeURIComponent(updated.edit_base_token)}`, {
            headers: apiService.getAuthHeaders(),
          });
          if (!response.ok) throw new Error(`读取已合并正文失败（HTTP ${response.status}）`);
          savedContent = record.kind === 'markdown' ? await response.text() : await response.arrayBuffer();
        }
        const latest = records.get(key);
        if (!latest || latest.generation === generation) {
          records.delete(key);
          await removePersisted(key);
          publishSaved({ key, file: updated, content: savedContent, generation });
          return { ...updated, outbox_generation: generation };
        }
        // Newer input has not incorporated a merge yet. Keep its original base.
        if (!updated.session_auto_merged) {
          latest.file = { ...updated, session_id: record.sessionId, outbox_generation: undefined };
        }
        latest.status = 'pending';
        records.set(key, latest);
        void persist(latest);
      } catch (error) {
        const latest = records.get(key);
        if (latest) {
          const status = typeof (error as { status?: unknown })?.status === 'number'
            ? Number((error as { status: number }).status)
            : 0;
          const code = typeof (error as { code?: unknown })?.code === 'string'
            ? String((error as { code: string }).code)
            : '';
          const revisionConflict = code === 'REVISION_CONFLICT'
            || code === 'SESSION_FILE_REVISION_CONFLICT';
          latest.status = revisionConflict
            ? 'conflict'
            : code === 'SESSION_FILE_BUSY' || code === 'SESSION_EDIT_RETRY' || status === 0 || status >= 500
              ? 'retry'
              : 'retained';
          records.set(key, latest);
          latest.errorMessage = revisionConflict
            ? '本地草稿基于旧版本且缺少可验证的编辑基线，已保留且不会覆盖当前文件。'
            : error instanceof Error ? error.message : '保存暂时未完成，草稿已保留。';
          void persist(latest);
          if (latest.status === 'retry') scheduleRetry(key);
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

export function flushSessionDraftInBackground(key: string): void {
  void flushSessionDraft(key).catch(() => undefined);
}

export async function startSessionDraftOutbox(): Promise<void> {
  await hydrate();
  records.forEach((record, key) => {
    if (record.status === 'pending' || record.status === 'retry') {
      flushSessionDraftInBackground(key);
    }
  });
}

export async function discardSessionDraft(sessionId: string, path: string): Promise<void> {
  const key = sessionDraftKey(sessionId, path);
  records.delete(key);
  const timer = retryTimers.get(key);
  if (timer !== undefined) window.clearTimeout(timer);
  retryTimers.delete(key);
  await removePersisted(key);
}

export async function discardSessionDrafts(sessionId: string): Promise<void> {
  await hydrate();
  const keys = [...records.values()]
    .filter((record) => record.sessionId === sessionId)
    .map((record) => record.key);
  keys.forEach((key) => {
    records.delete(key);
    generations.delete(key);
    savedSnapshots.delete(key);
    const timer = retryTimers.get(key);
    if (timer !== undefined) window.clearTimeout(timer);
    retryTimers.delete(key);
  });
  await Promise.all(keys.map(removePersisted));
}

export function resetSessionDraftOutboxForTests(): void {
  retryTimers.forEach((timer) => window.clearTimeout(timer));
  retryTimers.clear();
  records.clear();
  generations.clear();
  savedSnapshots.clear();
  savedListeners.clear();
  inFlight.clear();
  hydrationPromise = null;
  persistenceTail = Promise.resolve();
}
