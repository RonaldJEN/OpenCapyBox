import { beforeEach, describe, expect, it, vi } from 'vitest';

const mocks = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
  put: vi.fn(),
  patch: vi.fn(),
  delete: vi.fn(),
}));

vi.mock('../../services/api', () => ({
  apiService: {
    getAxiosClient: () => mocks,
    getAuthHeaders: () => ({ Authorization: 'Bearer token' }),
  },
}));

import {
  WorkspaceApiError,
  workspaceApi,
} from '../../services/workspaceApi';
import type { WorkspaceEntry } from '../../types/workspace';

const entry: WorkspaceEntry = {
  entry_id: 'entry-1',
  parent_id: null,
  name: 'report.md',
  kind: 'file',
  path: 'report.md',
  size_bytes: 12,
  mime_type: 'text/markdown',
  sha256: 'abc',
  revision: 7,
  current_version_id: 'version-7',
  status: 'active',
  created_at: '2026-08-26T00:00:00Z',
  updated_at: '2026-08-26T00:00:00Z',
};

describe('workspaceApi', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('根目录列表省略 parent_id，并传递游标', async () => {
    mocks.get.mockResolvedValueOnce({
      data: { items: [entry], next_cursor: 'next', workspace_revision: 3 },
    });

    await expect(workspaceApi.listEntries({ cursor: 'cursor' })).resolves.toEqual({
      items: [entry],
      next_cursor: 'next',
      workspace_revision: 3,
    });
    expect(mocks.get).toHaveBeenCalledWith('/workspace/entries', {
      params: { limit: 100, cursor: 'cursor' },
    });
  });

  it('listAllEntries 按 opaque cursor 拉完所有页面且不永久漏项', async () => {
    const second = { ...entry, entry_id: 'entry-2', name: 'second.md' };
    mocks.get
      .mockResolvedValueOnce({ data: { items: [entry], next_cursor: 'page-2', workspace_revision: 3 } })
      .mockResolvedValueOnce({ data: { items: [second], next_cursor: null, workspace_revision: 4 } });

    await expect(workspaceApi.listAllEntries({ parentId: 'dir-1' })).resolves.toEqual({
      items: [entry, second], next_cursor: null, workspace_revision: 4,
    });
    expect(mocks.get).toHaveBeenNthCalledWith(2, '/workspace/entries', {
      params: { limit: 200, parent_id: 'dir-1', cursor: 'page-2' },
    });
  });

  it('按稳定 entry id 解析深链，并为 Markdown 相对资源生成 path URL', async () => {
    mocks.get.mockResolvedValueOnce({ data: entry });
    await expect(workspaceApi.getEntry('entry-1')).resolves.toEqual(entry);
    expect(mocks.get).toHaveBeenCalledWith('/workspace/entries/entry-1');
    expect(workspaceApi.contentPathUrl('reports/assets/chart.png')).toBe(
      '/api/workspace/content?path=reports%2Fassets%2Fchart.png&preview=true',
    );
  });

  it('当前内容走 entry 路由，固定版本只走 version 路由', () => {
    expect(workspaceApi.previewContentUrl('entry-1')).toBe(
      '/api/workspace/entries/entry-1/content?preview=true',
    );
    expect(workspaceApi.previewContentUrl('entry-1', 'version-7')).toBe(
      '/api/workspace/versions/version-7/content?preview=true',
    );
  });

  it('内容更新发送 raw body 和带引号的 If-Match revision', async () => {
    mocks.put.mockResolvedValueOnce({
      data: { status: 'UPDATED', entry: { ...entry, revision: 8 }, mutation_id: 'mutation-1' },
    });

    await workspaceApi.updateContent(entry, '# updated', 'text/markdown; charset=utf-8');

    expect(mocks.put).toHaveBeenCalledWith(
      '/workspace/entries/entry-1/content',
      '# updated',
      {
        headers: expect.objectContaining({
          'Content-Type': 'text/markdown; charset=utf-8',
          'If-Match': '"7"',
          'X-Workspace-Base-Version': 'version-7',
          'Idempotency-Key': expect.stringMatching(/^update-content:/),
        }),
      },
    );
  });

  it('检查点只提升指定 current version，不重新上传文件内容', async () => {
    mocks.post.mockResolvedValueOnce({
      data: {
        version_id: 'version-7',
        entry_id: 'entry-1',
        sequence: 7,
        checkpoint_kind: 'web_idle',
      },
    });

    await workspaceApi.checkpoint('entry-1', 7, 'version-7', 'web_idle');

    expect(mocks.post).toHaveBeenCalledWith(
      '/workspace/entries/entry-1/checkpoint',
      {
        expected_revision: 7,
        version_id: 'version-7',
        checkpoint_kind: 'web_idle',
      },
    );
  });

  it('Session 导入保持后端定义的固定请求形状和 opaque source revision', async () => {
    mocks.post.mockResolvedValueOnce({
      data: { status: 'CREATED', entry, mutation_id: 'mutation-2' },
    });
    const request = {
      session_id: 'session-1',
      source_path: 'reports/report.md',
      source_revision: 'v1:12:999',
      destination_parent_id: null,
      destination_name: 'report.md',
      conflict_policy: 'fail' as const,
      expected_destination_revision: null,
      idempotency_key: 'import:key',
    };

    await workspaceApi.importSessionFile(request);

    expect(mocks.post).toHaveBeenCalledWith('/workspace/imports/session-file', request);
  });

  it('直接批量删除只提交稳定 ID、revision 和幂等键', async () => {
    const result = { status: 'DELETED', mutation_id: 'd1', affected_entry_ids: ['entry-1'], root_count: 1, entry_count: 1 };
    mocks.post.mockResolvedValueOnce({ data: result });
    await expect(workspaceApi.deleteEntries([entry], 'delete-intent')).resolves.toEqual(result);
    expect(mocks.post).toHaveBeenCalledWith('/workspace/entries/delete-batch', {
      items: [{ entry_id: 'entry-1', expected_revision: 7 }],
      idempotency_key: 'delete-intent',
    });
  });

  it('把结构化 409 转换为可判别的 WorkspaceApiError', async () => {
    mocks.post.mockRejectedValueOnce({
      isAxiosError: true,
      message: 'Request failed',
      response: {
        status: 409,
        data: { detail: { code: 'NAME_CONFLICT', message: 'duplicate', entry } },
      },
    });

    const error = await workspaceApi.createFile(null, 'report.md', 'markdown').catch((caught) => caught);

    expect(error).toBeInstanceOf(WorkspaceApiError);
    expect(error).toMatchObject({ status: 409, detail: { code: 'NAME_CONFLICT', entry } });
  });
});
