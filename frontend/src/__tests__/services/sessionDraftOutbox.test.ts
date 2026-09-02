import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { FileInfo } from '../../types';
import { apiService } from '../../services/api';
import {
  flushSessionDraft,
  getSessionDraft,
  getSessionSaveSnapshot,
  queueSessionMarkdownDraft,
  resetSessionDraftOutboxForTests,
  startSessionDraftOutbox,
} from '../../services/sessionDraftOutbox';

vi.mock('../../services/api', () => ({
  apiService: {
    updateSessionMarkdown: vi.fn(),
    updateSessionSpreadsheet: vi.fn(),
    getAuthHeaders: () => ({}),
  },
}));

describe('sessionDraftOutbox revision conflicts', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    resetSessionDraftOutboxForTests();
  });
  afterEach(() => { vi.unstubAllGlobals(); });

  it('合并正文读取期间续写保留原基线，最终回执包含合并正文且 generation 不复用', async () => {
    const file: FileInfo = { name: 'merge.md', path: 'merge.md', size: 10, modified: 'before', revision: 'v1:10:1', type: 'md', edit_base_token: 'base-1' };
    const first = { ...file, revision: 'v1:20:2', edit_base_token: 'base-2', session_auto_merged: true };
    const second = { ...first, revision: 'v1:30:3', edit_base_token: 'base-3' };
    let finishRead!: (value: Response) => void;
    const delayedRead = new Promise<Response>((resolve) => { finishRead = resolve; });
    vi.stubGlobal('fetch', vi.fn().mockReturnValueOnce(delayedRead).mockResolvedValueOnce({ ok: true, text: async () => 'human2 + remote' }));
    vi.mocked(apiService.updateSessionMarkdown).mockResolvedValueOnce(first).mockResolvedValueOnce(second);
    const draft = queueSessionMarkdownDraft('s1', file, 'human1');
    const saving = flushSessionDraft(draft.key);
    await vi.waitFor(() => expect(fetch).toHaveBeenCalledTimes(1));
    const newer = queueSessionMarkdownDraft('s1', file, 'human2');
    finishRead({ ok: true, text: async () => 'human1 + remote' } as Response);
    await saving;
    expect(apiService.updateSessionMarkdown).toHaveBeenNthCalledWith(2, 's1', expect.objectContaining({ edit_base_token: 'base-1' }), 'human2', newer.saveId);
    expect(getSessionSaveSnapshot('s1', file.path)?.content).toBe('human2 + remote');
    const next = queueSessionMarkdownDraft('s1', second, 'human3 + remote');
    expect(next.generation).toBeGreaterThan(newer.generation);
  });

  it('版本冲突保留草稿但停止自动重试', async () => {
    vi.mocked(apiService.updateSessionMarkdown).mockRejectedValue({
      status: 409,
      code: 'SESSION_FILE_REVISION_CONFLICT',
    });
    const queued = queueSessionMarkdownDraft(
      'session-conflict',
      {
        source: 'session',
        session_id: 'session-conflict',
        name: 'report.md',
        path: 'report.md',
        size: 10,
        modified: '2026-08-28T10:00:00Z',
        revision: 'v1:10:100',
        type: 'md',
      },
      'local draft',
    );

    await expect(flushSessionDraft(queued.key)).rejects.toBeTruthy();
    expect((await getSessionDraft('session-conflict', 'report.md'))?.status).toBe('conflict');

    await startSessionDraftOutbox();
    await Promise.resolve();
    expect(apiService.updateSessionMarkdown).toHaveBeenCalledTimes(1);
  });

  it('SESSION_FILE_BUSY 保持 retry，启动 outbox 后可继续同步', async () => {
    vi.mocked(apiService.updateSessionMarkdown)
      .mockRejectedValueOnce({ status: 409, code: 'SESSION_FILE_BUSY' })
      .mockResolvedValueOnce({
        name: 'busy.md', path: 'busy.md', size: 12, modified: 'later',
        revision: 'v1:12:200', type: 'md',
      });
    const queued = queueSessionMarkdownDraft('session-busy', {
      name: 'busy.md', path: 'busy.md', size: 8, modified: 'before',
      revision: 'v1:8:100', type: 'md',
    }, 'local draft');

    await expect(flushSessionDraft(queued.key)).rejects.toBeTruthy();
    expect((await getSessionDraft('session-busy', 'busy.md'))?.status).toBe('retry');
    await startSessionDraftOutbox();
    await vi.waitFor(() => expect(apiService.updateSessionMarkdown).toHaveBeenCalledTimes(2));
    expect(await getSessionDraft('session-busy', 'busy.md')).toBeNull();
  });
});
