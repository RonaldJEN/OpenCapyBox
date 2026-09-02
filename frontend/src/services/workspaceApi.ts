import axios from 'axios';

import type { FileInfo } from '../types';
import type {
  WorkspaceEntriesResponse,
  WorkspaceEntry,
  WorkspaceDeleteResult,
  WorkspaceErrorDetail,
  WorkspaceImportSessionFileRequest,
  WorkspaceMutationResult,
  WorkspaceVersion,
} from '../types/workspace';
import { apiService } from './api';

export class WorkspaceApiError extends Error {
  constructor(
    public readonly status: number | undefined,
    public readonly detail: WorkspaceErrorDetail,
  ) {
    super(detail.message);
    this.name = 'WorkspaceApiError';
  }
}

function asWorkspaceApiError(error: unknown): WorkspaceApiError {
  if (error instanceof WorkspaceApiError) return error;
  if (axios.isAxiosError(error)) {
    const status = error.response?.status;
    const rawDetail = error.response?.data?.detail;
    if (rawDetail && typeof rawDetail === 'object') {
      return new WorkspaceApiError(status, {
        code: String(rawDetail.code || 'WORKSPACE_REQUEST_FAILED'),
        message: String(rawDetail.message || error.message || '工作区操作失败'),
        entry: rawDetail.entry as WorkspaceEntry | undefined,
        current_revision: rawDetail.current_revision == null
          ? undefined
          : String(rawDetail.current_revision),
        mutation_id: rawDetail.mutation_id == null
          ? undefined
          : String(rawDetail.mutation_id),
        mutation_state: rawDetail.mutation_state == null
          ? undefined
          : String(rawDetail.mutation_state),
        outcome: rawDetail.outcome == null
          ? undefined
          : String(rawDetail.outcome),
      });
    }
    return new WorkspaceApiError(status, {
      code: 'WORKSPACE_REQUEST_FAILED',
      message: error.message || '工作区操作失败',
    });
  }
  return new WorkspaceApiError(undefined, {
    code: 'WORKSPACE_REQUEST_FAILED',
    message: error instanceof Error ? error.message : '工作区操作失败',
  });
}

export function createWorkspaceIdempotencyKey(prefix: string): string {
  const random = typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function'
    ? crypto.randomUUID()
    : `${Date.now()}-${Math.random().toString(16).slice(2)}`;
  return `${prefix}:${random}`;
}

export function workspaceEntryToFileInfo(entry: WorkspaceEntry): FileInfo {
  const extension = entry.name.includes('.') ? entry.name.split('.').pop() || '' : '';
  return {
    source: 'workspace',
    entry_id: entry.entry_id,
    workspace_path: entry.path,
    name: entry.name,
    path: entry.path,
    revision: String(entry.revision),
    version_id: entry.current_version_id || undefined,
    tree_revision: entry.tree_revision,
    size: entry.size_bytes,
    modified: entry.updated_at,
    type: extension.toLowerCase(),
    is_directory: entry.kind === 'directory',
  };
}

class WorkspaceApi {
  private get client() {
    return apiService.getAxiosClient();
  }

  contentUrl(entryId: string): string {
    return `/api/workspace/entries/${encodeURIComponent(entryId)}/content`;
  }

  previewContentUrl(entryId: string, versionId?: string | null): string {
    return versionId
      ? this.versionContentUrl(versionId, true)
      : `${this.contentUrl(entryId)}?preview=true`;
  }

  versionContentUrl(versionId: string, preview = false): string {
    const base = `/api/workspace/versions/${encodeURIComponent(versionId)}/content`;
    return preview ? `${base}?preview=true` : base;
  }

  contentPathUrl(path: string): string {
    return `/api/workspace/content?path=${encodeURIComponent(path)}&preview=true`;
  }

  async listEntries(options: {
    parentId?: string | null;
    query?: string;
    cursor?: string | null;
    limit?: number;
  } = {}): Promise<WorkspaceEntriesResponse> {
    try {
      const params: Record<string, string | number | boolean> = {
        limit: options.limit ?? 100,
      };
      if (options.parentId) params.parent_id = options.parentId;
      if (options.query?.trim()) params.q = options.query.trim();
      if (options.cursor) params.cursor = options.cursor;
      const response = await this.client.get<WorkspaceEntriesResponse>('/workspace/entries', { params });
      return response.data;
    } catch (error) {
      throw asWorkspaceApiError(error);
    }
  }

  async listAllEntries(options: {
    parentId?: string | null;
    query?: string;
  } = {}): Promise<WorkspaceEntriesResponse> {
    const items: WorkspaceEntry[] = [];
    let cursor: string | null = null;
    let workspaceRevision = 0;
    do {
      const page = await this.listEntries({ ...options, cursor, limit: 200 });
      items.push(...page.items);
      workspaceRevision = page.workspace_revision;
      cursor = page.next_cursor;
    } while (cursor);
    return { items, next_cursor: null, workspace_revision: workspaceRevision };
  }

  async getEntry(entryId: string): Promise<WorkspaceEntry> {
    try {
      const response = await this.client.get<WorkspaceEntry>(
        `/workspace/entries/${encodeURIComponent(entryId)}`,
      );
      return response.data;
    } catch (error) {
      throw asWorkspaceApiError(error);
    }
  }

  async checkpoint(
    entryId: string,
    expectedRevision: number,
    versionId: string,
    checkpointKind: 'web_idle' | 'web_close' | 'web_periodic',
  ): Promise<WorkspaceVersion> {
    try {
      const response = await this.client.post<WorkspaceVersion>(
        `/workspace/entries/${encodeURIComponent(entryId)}/checkpoint`,
        {
          expected_revision: expectedRevision,
          version_id: versionId,
          checkpoint_kind: checkpointKind,
        },
      );
      return response.data;
    } catch (error) {
      throw asWorkspaceApiError(error);
    }
  }

  async createDirectory(parentId: string | null, name: string): Promise<WorkspaceMutationResult> {
    try {
      const response = await this.client.post<WorkspaceMutationResult>('/workspace/directories', {
        parent_id: parentId,
        name,
        idempotency_key: createWorkspaceIdempotencyKey('create-directory'),
      });
      return response.data;
    } catch (error) {
      throw asWorkspaceApiError(error);
    }
  }

  async createFile(
    parentId: string | null,
    name: string,
    fileType: 'markdown' | 'xlsx',
  ): Promise<WorkspaceMutationResult> {
    try {
      const response = await this.client.post<WorkspaceMutationResult>('/workspace/files', {
        parent_id: parentId,
        name,
        file_type: fileType,
        idempotency_key: createWorkspaceIdempotencyKey(`create-${fileType}`),
      });
      return response.data;
    } catch (error) {
      throw asWorkspaceApiError(error);
    }
  }

  async uploadFile(parentId: string | null, file: File): Promise<WorkspaceMutationResult> {
    try {
      const formData = new FormData();
      formData.append('file', file);
      if (parentId) formData.append('parent_id', parentId);
      formData.append('idempotency_key', createWorkspaceIdempotencyKey('upload'));
      const response = await this.client.post<WorkspaceMutationResult>('/workspace/uploads', formData, {
        headers: { 'Content-Type': 'multipart/form-data' },
      });
      return response.data;
    } catch (error) {
      throw asWorkspaceApiError(error);
    }
  }

  async updateContent(
    entry: WorkspaceEntry,
    content: string | ArrayBuffer,
    contentType: string,
    idempotencyKey?: string,
  ): Promise<WorkspaceMutationResult> {
    try {
      const response = await this.client.put<WorkspaceMutationResult>(
        `/workspace/entries/${encodeURIComponent(entry.entry_id)}/content`,
        content,
        {
          headers: {
            'Content-Type': contentType,
            'If-Match': `"${entry.revision}"`,
            ...(entry.current_version_id
              ? { 'X-Workspace-Base-Version': entry.current_version_id }
              : {}),
            'Idempotency-Key': idempotencyKey || createWorkspaceIdempotencyKey('update-content'),
          },
        },
      );
      return response.data;
    } catch (error) {
      throw asWorkspaceApiError(error);
    }
  }

  async updateEntry(
    entry: WorkspaceEntry,
    update: { parentId?: string | null; name?: string },
  ): Promise<WorkspaceMutationResult> {
    try {
      const response = await this.client.patch<WorkspaceMutationResult>(
        `/workspace/entries/${encodeURIComponent(entry.entry_id)}`,
        {
          ...(update.parentId !== undefined ? { parent_id: update.parentId } : {}),
          ...(update.name !== undefined ? { name: update.name } : {}),
          expected_revision: entry.revision,
          idempotency_key: createWorkspaceIdempotencyKey('update-entry'),
        },
      );
      return response.data;
    } catch (error) {
      throw asWorkspaceApiError(error);
    }
  }

  async deleteEntries(entries: WorkspaceEntry[], idempotencyKey: string): Promise<WorkspaceDeleteResult> {
    try {
      const response = await this.client.post<WorkspaceDeleteResult>('/workspace/entries/delete-batch', {
        items: entries.map((entry) => ({
          entry_id: entry.entry_id,
          expected_revision: entry.revision,
        })),
        idempotency_key: idempotencyKey,
      });
      return response.data;
    } catch (error) {
      throw asWorkspaceApiError(error);
    }
  }

  async importSessionFile(request: WorkspaceImportSessionFileRequest): Promise<WorkspaceMutationResult> {
    try {
      const response = await this.client.post<WorkspaceMutationResult>(
        '/workspace/imports/session-file',
        request,
      );
      return response.data;
    } catch (error) {
      throw asWorkspaceApiError(error);
    }
  }

  async download(entry: WorkspaceEntry): Promise<void> {
    try {
      const response = await fetch(this.contentUrl(entry.entry_id), {
        headers: apiService.getAuthHeaders(),
      });
      if (!response.ok) throw new Error(`下载失败（HTTP ${response.status}）`);
      const blob = await response.blob();
      const objectUrl = URL.createObjectURL(blob);
      try {
        const link = document.createElement('a');
        link.href = objectUrl;
        link.download = entry.name;
        document.body.appendChild(link);
        link.click();
        link.remove();
      } finally {
        URL.revokeObjectURL(objectUrl);
      }
    } catch (error) {
      throw asWorkspaceApiError(error);
    }
  }

}

export const workspaceApi = new WorkspaceApi();
