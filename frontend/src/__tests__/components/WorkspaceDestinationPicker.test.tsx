import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const client = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
  put: vi.fn(),
  patch: vi.fn(),
  delete: vi.fn(),
}));

vi.mock('../../services/api', () => ({
  apiService: {
    getAxiosClient: () => client,
    getAuthHeaders: () => ({ Authorization: 'Bearer token' }),
  },
}));

import { WorkspaceDestinationPicker } from '../../components/workspace/WorkspaceDestinationPicker';
import type { WorkspaceEntry } from '../../types/workspace';

const directory: WorkspaceEntry = {
  entry_id: 'dir-1',
  parent_id: null,
  name: '研究',
  kind: 'directory',
  path: '研究',
  size_bytes: 0,
  mime_type: 'inode/directory',
  sha256: '',
  revision: 1,
  status: 'active',
  created_at: '2026-08-26T00:00:00Z',
  updated_at: '2026-08-26T00:00:00Z',
};

const importedEntry: WorkspaceEntry = {
  ...directory,
  entry_id: 'file-1',
  name: 'report.pdf',
  kind: 'file',
  path: '研究/report.pdf',
  size_bytes: 100,
  mime_type: 'application/pdf',
  sha256: 'hash',
};

describe('WorkspaceDestinationPicker', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    client.get.mockResolvedValue({
      data: { items: [directory], next_cursor: null, workspace_revision: 1 },
    });
  });

  it('使用 Session 返回的 opaque revision 导入到选中目录', async () => {
    client.post.mockResolvedValueOnce({
      data: { status: 'CREATED', entry: importedEntry, mutation_id: 'mutation-1' },
    });
    const onImported = vi.fn();
    render(
      <WorkspaceDestinationPicker
        open
        sessionId="session-1"
        sourceFile={{
          name: 'report.pdf',
          path: 'reports/report.pdf',
          size: 100,
          modified: '2026-08-26T00:00:00Z',
          revision: 'v1:100:123456',
          type: 'pdf',
        }}
        onClose={vi.fn()}
        onImported={onImported}
      />,
    );

    fireEvent.click(await screen.findByRole('button', { name: '研究' }));
    fireEvent.click(screen.getByRole('button', { name: '确定' }));

    await waitFor(() => {
      expect(client.post).toHaveBeenCalledWith('/workspace/imports/session-file', expect.objectContaining({
        session_id: 'session-1',
        source_path: 'reports/report.pdf',
        source_revision: 'v1:100:123456',
        destination_parent_id: 'dir-1',
        destination_name: 'report.pdf',
        conflict_policy: 'fail',
        expected_destination_revision: null,
        idempotency_key: expect.stringMatching(/^import-session-file:/),
      }));
    });
    expect(onImported).toHaveBeenCalledWith(expect.objectContaining({ entry: importedEntry }));
  });

  it('同名冲突后覆盖时携带目标 revision', async () => {
    const conflictEntry = { ...importedEntry, revision: 9 };
    client.post
      .mockRejectedValueOnce({
        isAxiosError: true,
        message: 'conflict',
        response: {
          status: 409,
          data: { detail: { code: 'NAME_CONFLICT', message: 'duplicate', entry: conflictEntry } },
        },
      })
      .mockResolvedValueOnce({
        data: { status: 'UPDATED', entry: { ...conflictEntry, revision: 10 }, mutation_id: 'mutation-2' },
      });

    render(
      <WorkspaceDestinationPicker
        open
        sessionId="session-1"
        sourceFile={{ name: 'report.pdf', path: 'report.pdf', size: 100, modified: 'now', revision: 'opaque', type: 'pdf' }}
        onClose={vi.fn()}
        onImported={vi.fn()}
      />,
    );

    fireEvent.click(screen.getByRole('button', { name: '确定' }));
    expect(await screen.findByText('目标目录已有同名文件')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '覆盖文件' }));

    await waitFor(() => expect(client.post).toHaveBeenCalledTimes(2));
    expect(client.post.mock.calls[1][1]).toMatchObject({
      conflict_policy: 'overwrite',
      expected_destination_revision: 9,
    });
  });

  it('缺少 source revision 时阻止导入并给出明确提示', async () => {
    render(
      <WorkspaceDestinationPicker
        open
        sessionId="session-1"
        sourceFile={{ name: 'report.pdf', path: 'report.pdf', size: 100, modified: 'now', type: 'pdf' }}
        onClose={vi.fn()}
        onImported={vi.fn()}
      />,
    );

    fireEvent.click(screen.getByRole('button', { name: '确定' }));

    expect(await screen.findByRole('alert')).toHaveTextContent('文件版本尚未就绪');
    expect(client.post).not.toHaveBeenCalled();
  });
});
