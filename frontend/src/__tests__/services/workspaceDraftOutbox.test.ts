import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { FileInfo } from '../../types';
import { WorkspaceApiError, workspaceApi } from '../../services/workspaceApi';
import {
  flushWorkspaceDraft,
  discardWorkspaceDrafts,
  getWorkspaceDraft,
  queueWorkspaceMarkdownDraft,
  resetWorkspaceDraftOutboxForTests,
  subscribeWorkspaceDraftLosses,
} from '../../services/workspaceDraftOutbox';


const file: FileInfo = {
  name: 'draft.md',
  path: 'draft.md',
  type: 'file',
  size: 4,
  modified: '2026-08-28T00:00:00Z',
  source: 'workspace',
  entry_id: 'entry-1',
  revision: 1,
  version_id: 'version-1',
};


describe('workspaceDraftOutbox checkpoints', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    resetWorkspaceDraftOutboxForTests();
  });

  afterEach(() => {
    vi.restoreAllMocks();
    resetWorkspaceDraftOutboxForTests();
    vi.useRealTimers();
  });

  it.each([true, false])('续写只在未自动合并时推进基线（auto_merged=%s）', async (autoMerged) => {
    const result = {
      status: 'UPDATED' as const,
      mutation_id: 'saved-1',
      auto_merged: autoMerged,
      entry: {
        entry_id: file.entry_id!, parent_id: null, name: file.name, kind: 'file' as const,
        path: file.path, size_bytes: 30, mime_type: 'text/markdown', sha256: 'merged-sha',
        revision: 3, current_version_id: 'version-3', status: 'active' as const,
        created_at: file.modified, updated_at: '2026-08-28T00:00:03Z',
      },
    };
    let release!: (value: typeof result) => void;
    const firstSave = new Promise<typeof result>((resolve) => { release = resolve; });
    const update = vi.spyOn(workspaceApi, 'updateContent')
      .mockImplementationOnce(() => firstSave)
      .mockResolvedValue({ ...result, mutation_id: 'saved-2' });
    const queued = queueWorkspaceMarkdownDraft(file, 'Human: edit1\nAI: old\n');
    const saving = flushWorkspaceDraft(queued.key);
    queueWorkspaceMarkdownDraft(file, 'Human: edit2\nAI: old\n');
    release(result);
    await saving;

    expect(update).toHaveBeenCalledTimes(2);
    expect(update.mock.calls[1][0]).toMatchObject({
      revision: autoMerged ? 1 : 3,
      current_version_id: autoMerged ? 'version-1' : 'version-3',
    });
    expect(update.mock.calls[1][1]).toBe('Human: edit2\nAI: old\n');
  });

  it('先持久保存 head，停止编辑 30 秒后再提升同一 revision 为检查点', async () => {
    vi.spyOn(workspaceApi, 'updateContent').mockResolvedValue({
      status: 'UPDATED',
      mutation_id: 'mutation-1',
      auto_merged: false,
      entry: {
        entry_id: 'entry-1',
        parent_id: null,
        name: 'draft.md',
        kind: 'file',
        path: 'draft.md',
        size_bytes: 7,
        mime_type: 'text/markdown',
        sha256: 'sha',
        revision: 2,
        current_version_id: 'version-2',
        tree_revision: 1,
        status: 'active',
        created_at: '2026-08-28T00:00:00Z',
        updated_at: '2026-08-28T00:00:01Z',
      },
    });
    const checkpoint = vi.spyOn(workspaceApi, 'checkpoint').mockResolvedValue({
      version_id: 'version-2',
      entry_id: 'entry-1',
      sequence: 2,
      parent_version_id: 'version-1',
      restored_from_version_id: null,
      sha256: 'sha',
      size_bytes: 7,
      mime_type: 'text/markdown',
      actor: 'web',
      state: 'materialized',
      pinned: false,
      checkpoint_kind: 'web_idle',
      created_at: '2026-08-28T00:00:01Z',
    });

    const queued = queueWorkspaceMarkdownDraft(file, 'updated');
    await flushWorkspaceDraft(queued.key);
    expect(checkpoint).not.toHaveBeenCalled();

    await vi.advanceTimersByTimeAsync(30_000);
    expect(checkpoint).toHaveBeenCalledWith('entry-1', 2, 'version-2', 'web_idle');
  });

  it('删除成功清掉未保存草稿，迟到 PUT 不再复活条目或安排检查点', async () => {
    let release!: (value: any) => void;
    vi.spyOn(workspaceApi, 'updateContent').mockImplementation(() => new Promise((resolve) => { release = resolve; }));
    const queued = queueWorkspaceMarkdownDraft(file, 'will be deleted');
    const saving = flushWorkspaceDraft(queued.key);
    const rejected = expect(saving).rejects.toThrow('工作区文件已删除');
    await discardWorkspaceDrafts([file.entry_id!]);
    release({ status: 'UPDATED', mutation_id: 'late', entry: {
      entry_id: file.entry_id!, parent_id: null, name: file.name, kind: 'file', path: file.path,
      revision: 2, current_version_id: 'late-version', status: 'active',
      size_bytes: 12, mime_type: null, sha256: 'sha', created_at: 'now', updated_at: 'later',
    } });
    await rejected;
    expect(await getWorkspaceDraft(file.entry_id!)).toBeNull();
    expect(() => queueWorkspaceMarkdownDraft(file, 'late editor')).toThrow('工作区文件已删除');
  });

  it('只有处理中错误使用同一 idempotency key 延迟重试', async () => {
    const result = {
      status: 'UPDATED' as const,
      mutation_id: 'saved-after-pending',
      auto_merged: false,
      entry: {
        entry_id: file.entry_id!, parent_id: null, name: file.name, kind: 'file' as const,
        path: file.path, size_bytes: 7, mime_type: 'text/markdown', sha256: 'sha',
        revision: 2, current_version_id: 'version-2', tree_revision: 1, status: 'active' as const,
        created_at: file.modified, updated_at: '2026-08-28T00:00:01Z',
      },
    };
    const update = vi.spyOn(workspaceApi, 'updateContent')
      .mockRejectedValueOnce(new WorkspaceApiError(409, {
        code: 'MUTATION_IN_PROGRESS',
        message: '仍在处理中',
        mutation_state: 'prepared',
        outcome: 'pending',
      }))
      .mockResolvedValue(result);
    const queued = queueWorkspaceMarkdownDraft(file, 'updated');

    await expect(flushWorkspaceDraft(queued.key)).rejects.toThrow('仍在处理中');
    expect((await getWorkspaceDraft(file.entry_id!))?.status).toBe('retry');
    await vi.advanceTimersByTimeAsync(1800);
    await Promise.resolve();

    expect(update).toHaveBeenCalledTimes(2);
    expect(update.mock.calls[0][3]).toBe(queued.idempotencyKey);
    expect(update.mock.calls[1][3]).toBe(queued.idempotencyKey);
    expect(await getWorkspaceDraft(file.entry_id!)).toBeNull();
  });

  it('终态失败停止重试、丢弃 Workspace 草稿并发布文件内提示', async () => {
    const lost = vi.fn();
    const unsubscribe = subscribeWorkspaceDraftLosses(lost);
    const update = vi.spyOn(workspaceApi, 'updateContent').mockRejectedValue(
      new WorkspaceApiError(409, {
        code: 'DESTINATION_CHANGED',
        message: '目标文件已变化',
        mutation_state: 'failed',
        outcome: 'not_applied',
      }),
    );
    const queued = queueWorkspaceMarkdownDraft(file, 'will be dropped');

    await expect(flushWorkspaceDraft(queued.key)).rejects.toThrow('目标文件已变化');
    expect(await getWorkspaceDraft(file.entry_id!)).toBeNull();
    expect(lost).toHaveBeenCalledWith(expect.objectContaining({
      entryId: file.entry_id,
      message: '刚才的修改未保存，已恢复到最近保存版本。',
    }));
    await vi.advanceTimersByTimeAsync(60_000);
    expect(update).toHaveBeenCalledTimes(1);
    unsubscribe();
  });
});
