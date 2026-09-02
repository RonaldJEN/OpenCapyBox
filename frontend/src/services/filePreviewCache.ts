interface CachedPreviewValue<T = unknown> {
  value: T;
  workspaceEntryId?: string;
}

const PREVIEW_CACHE_LIMIT = 30;
const previewCache = new Map<string, CachedPreviewValue>();
const workspaceKeys = new Map<string, Set<string>>();

function forgetWorkspaceKey(entryId: string | undefined, key: string): void {
  if (!entryId) return;
  const keys = workspaceKeys.get(entryId);
  if (!keys) return;
  keys.delete(key);
  if (keys.size === 0) workspaceKeys.delete(entryId);
}

function deleteKey(key: string): void {
  const cached = previewCache.get(key);
  if (!cached) return;
  previewCache.delete(key);
  forgetWorkspaceKey(cached.workspaceEntryId, key);
}

export function readFilePreviewCache<T>(key: string): T | null {
  const cached = previewCache.get(key) as CachedPreviewValue<T> | undefined;
  if (!cached) return null;
  previewCache.delete(key);
  previewCache.set(key, cached);
  return cached.value;
}

export function writeFilePreviewCache<T>(
  key: string,
  value: T,
  workspaceEntryId?: string,
): void {
  deleteKey(key);
  previewCache.set(key, { value, workspaceEntryId });
  if (workspaceEntryId) {
    const keys = workspaceKeys.get(workspaceEntryId) ?? new Set<string>();
    keys.add(key);
    workspaceKeys.set(workspaceEntryId, keys);
  }
  while (previewCache.size > PREVIEW_CACHE_LIMIT) {
    const oldestKey = previewCache.keys().next().value as string | undefined;
    if (!oldestKey) break;
    deleteKey(oldestKey);
  }
}

export function invalidateWorkspacePreviewCache(entryIds: Iterable<string>): void {
  for (const entryId of entryIds) {
    const keys = [...(workspaceKeys.get(entryId) ?? [])];
    keys.forEach(deleteKey);
    workspaceKeys.delete(entryId);
  }
}

export function resetFilePreviewCacheForTests(): void {
  previewCache.clear();
  workspaceKeys.clear();
}
