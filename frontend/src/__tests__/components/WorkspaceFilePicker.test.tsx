import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const client = vi.hoisted(() => ({ get: vi.fn() }));
vi.mock('../../services/api', () => ({ apiService: { getAxiosClient: () => client } }));

import { WorkspaceFilePicker } from '../../components/workspace/WorkspaceFilePicker';
import type { WorkspaceEntry } from '../../types/workspace';

const makeFile = (id: string, name: string): WorkspaceEntry => ({
  entry_id: id, parent_id: null, name, kind: 'file', path: name, size_bytes: 1,
  mime_type: 'text/markdown', sha256: id, revision: 1, status: 'active', created_at: 'now', updated_at: 'now',
});

const makeDirectory = (id: string, name: string, parentId: string | null = null): WorkspaceEntry => ({
  entry_id: id, parent_id: parentId, name, kind: 'directory', path: name, size_bytes: 0,
  mime_type: null, sha256: null, revision: 1, status: 'active', created_at: 'now', updated_at: 'now',
});

describe('WorkspaceFilePicker', () => {
  beforeEach(() => { vi.clearAllMocks(); });

  it('通过 cursor 拉完文件并多选返回稳定 entry identity', async () => {
    const one = makeFile('one', 'one.md');
    const two = makeFile('two', 'two.pptx');
    client.get
      .mockResolvedValueOnce({ data: { items: [one], next_cursor: 'next', workspace_revision: 1 } })
      .mockResolvedValueOnce({ data: { items: [two], next_cursor: null, workspace_revision: 1 } });
    const onConfirm = vi.fn();
    render(<WorkspaceFilePicker onBack={vi.fn()} onClose={vi.fn()} onConfirm={onConfirm} />);

    const markdownButton = await screen.findByRole('button', { name: 'one.md' });
    const presentationButton = await screen.findByRole('button', { name: 'two.pptx' });
    expect(markdownButton).toHaveAttribute('title', 'one.md');
    expect(markdownButton.querySelector('svg.lucide-file-text')).toHaveClass('shrink-0');
    expect(presentationButton).toHaveAttribute('title', 'two.pptx');
    expect(presentationButton.querySelector('svg.lucide-presentation')).toHaveClass('shrink-0');
    fireEvent.click(markdownButton);
    fireEvent.click(presentationButton);
    fireEvent.click(screen.getByRole('button', { name: '添加到对话' }));

    expect(onConfirm).toHaveBeenCalledWith([one, two]);
    expect(client.get).toHaveBeenNthCalledWith(2, '/workspace/entries', {
      params: { limit: 200, cursor: 'next' },
    });
  });

  it('选择文件夹时保留文件夹 identity，不在前端展开成文件', async () => {
    const directory = makeDirectory('folder', '研究');
    client.get.mockResolvedValue({ data: { items: [directory], next_cursor: null, workspace_revision: 1 } });
    const onConfirm = vi.fn();
    render(<WorkspaceFilePicker onBack={vi.fn()} onClose={vi.fn()} onConfirm={onConfirm} />);

    fireEvent.click(await screen.findByRole('button', { name: '研究' }));
    expect(screen.getByText('已选择 1 个项目')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '添加到对话' }));

    expect(onConfirm).toHaveBeenCalledWith([directory]);
    expect(client.get).toHaveBeenCalledTimes(1);
  });

  it('搜索展示文件和文件夹并保留选择结果', async () => {
    const file = makeFile('report', '报告.md');
    const directory = makeDirectory('folder', '报告资料');
    client.get.mockResolvedValue({ data: { items: [file, directory], next_cursor: null, workspace_revision: 1 } });
    render(<WorkspaceFilePicker onBack={vi.fn()} onClose={vi.fn()} onConfirm={vi.fn()} />);
    fireEvent.change(screen.getByLabelText('搜索工作区文件'), { target: { value: '报告' } });
    await waitFor(() => expect(client.get).toHaveBeenCalledWith('/workspace/entries', expect.objectContaining({ params: expect.objectContaining({ q: '报告' }) })));
    fireEvent.click(await screen.findByRole('button', { name: /报告.md/ }));
    fireEvent.click(await screen.findByRole('button', { name: /报告资料/ }));
    expect(screen.getByText('已选择 2 个项目')).toBeInTheDocument();
  });
});
