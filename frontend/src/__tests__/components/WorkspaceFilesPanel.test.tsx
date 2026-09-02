import { createRef, forwardRef, useImperativeHandle } from 'react';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const controls = vi.hoisted(() => ({ dirty: false, save: vi.fn(async (_path: string) => ({ ok: true, stale: false })) }));
const api = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn() }));

vi.mock('../../services/api', () => ({
  apiService: { getAxiosClient: () => api, getAuthHeaders: () => ({}) },
}));

vi.mock('../../components/FilePreview', () => ({
  FilePreview: forwardRef(function PreviewMock({ file, previewUrlBuilder, refreshInPlace, contextNotice, readOnly }: any, ref) {
    useImperativeHandle(ref, () => ({
      ownerSessionId: 'workspace:persistent',
      ownerEpoch: 0,
      path: file.path,
      isDirty: () => controls.dirty,
      saveDirty: async (owner: any) => ({ ...owner, path: file.path, ...(await controls.save(file.path)) }),
    }), [file.path]);
    return <div data-testid={`preview-${file.name}`} data-revision={file.revision} data-refresh-in-place={String(Boolean(refreshInPlace))} data-primary={previewUrlBuilder(file)} data-relative={previewUrlBuilder({ ...file, path: 'assets/chart.png' })} data-notice={contextNotice || ''} data-readonly={String(Boolean(readOnly))}><button type="button" onClick={() => { controls.dirty = true; }}>mark dirty</button></div>;
  }),
}));

import { WorkspaceFilesPanel, type WorkspaceFilesPanelHandle } from '../../components/workspace/WorkspaceFilesPanel';
import { emitWorkspaceInvalidation, emitWorkspaceMutation, resetWorkspaceEventsForTests } from '../../services/workspaceEvents';
import type { WorkspaceEntry } from '../../types/workspace';

const first: WorkspaceEntry = {
  entry_id: 'one', parent_id: null, name: 'one.md', kind: 'file', path: 'one.md', size_bytes: 1,
  mime_type: 'text/markdown', sha256: 'a', revision: 1, status: 'active', created_at: 'now', updated_at: 'now',
};
const second: WorkspaceEntry = { ...first, entry_id: 'two', name: 'two.xlsx', path: 'two.xlsx', mime_type: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet' };

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((resolvePromise) => { resolve = resolvePromise; });
  return { promise, resolve };
}

describe('WorkspaceFilesPanel', () => {
  beforeEach(() => {
    resetWorkspaceEventsForTests();
    controls.dirty = false;
    controls.save.mockReset().mockResolvedValue({ ok: true, stale: false });
    api.get.mockReset();
    api.post.mockReset();
    api.get.mockResolvedValue({ data: first });
  });

  it('保留多标签并为 Markdown 相对资源使用 authoritative path URL', () => {
    const onActivateEntry = vi.fn();
    const { rerender } = render(<WorkspaceFilesPanel onActivateEntry={onActivateEntry} target={{ ...first, current_version_id: 'version-one' }} isOpen isExpanded={false} onToggleExpanded={vi.fn()} onClose={vi.fn()} />);
    const originalPreview = screen.getByTestId('preview-one.md');
    expect(screen.getByTestId('workspace-files-panel')).toHaveClass('w-full', 'min-w-0');
    expect(screen.getByTestId('preview-one.md')).toHaveAttribute('data-primary', '/api/workspace/versions/version-one/content?preview=true');
    expect(screen.getByTestId('preview-one.md')).toHaveAttribute('data-relative', '/api/workspace/content?path=assets%2Fchart.png&preview=true');

    rerender(<WorkspaceFilesPanel onActivateEntry={onActivateEntry} target={second} isOpen isExpanded={false} onToggleExpanded={vi.fn()} onClose={vi.fn()} />);
    const markdownTab = screen.getByRole('tab', { name: 'one.md' });
    const spreadsheetTab = screen.getByRole('tab', { name: 'two.xlsx' });
    expect(markdownTab).toHaveAttribute('title', 'one.md');
    expect(markdownTab.querySelector('svg')).toHaveClass('lucide-file-text', 'shrink-0');
    expect(spreadsheetTab).toHaveAttribute('title', 'two.xlsx');
    expect(spreadsheetTab.querySelector('svg')).toHaveClass('lucide-file-spreadsheet', 'shrink-0');
    expect(spreadsheetTab).toHaveAttribute('aria-selected', 'true');
    fireEvent.click(markdownTab);
    expect(markdownTab).toHaveAttribute('aria-selected', 'true');
    expect(onActivateEntry).toHaveBeenLastCalledWith(expect.objectContaining({ entry_id: 'one' }), { replace: false });
    expect(screen.getByTestId('preview-one.md')).toBe(originalPreview);
    fireEvent.click(screen.getByRole('button', { name: '关闭 two.xlsx' }));
    expect(onActivateEntry).toHaveBeenCalledTimes(1);
  });


  it('关闭多标签中的当前文件后，同一文件的新 target 事件可以重新打开', async () => {
    const onActivateEntry = vi.fn();
    const { rerender } = render(<WorkspaceFilesPanel target={first} isOpen isExpanded={false} onToggleExpanded={vi.fn()} onClose={vi.fn()} />);
    rerender(<WorkspaceFilesPanel onActivateEntry={onActivateEntry} target={second} isOpen isExpanded={false} onToggleExpanded={vi.fn()} onClose={vi.fn()} />);
    fireEvent.click(screen.getByRole('button', { name: '关闭 two.xlsx' }));
    await waitFor(() => expect(screen.queryByRole('tab', { name: 'two.xlsx' })).not.toBeInTheDocument());
    expect(screen.getByRole('tab', { name: 'one.md' })).toHaveAttribute('aria-selected', 'true');
    expect(onActivateEntry).toHaveBeenCalledWith(first, { replace: true });

    rerender(<WorkspaceFilesPanel target={{ ...second }} isOpen isExpanded={false} onToggleExpanded={vi.fn()} onClose={vi.fn()} />);
    expect(screen.getByRole('tab', { name: 'two.xlsx' })).toHaveAttribute('aria-selected', 'true');
  });

  it('不向用户展示提案、发布或拒绝入口', () => {
    render(<WorkspaceFilesPanel target={first} isOpen isExpanded={false} onToggleExpanded={vi.fn()} onClose={vi.fn()} />);

    expect(screen.queryByText(/提案|待确认/)).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /发布|拒绝/ })).not.toBeInTheDocument();
  });

  it('公开 discriminated workspace owner，并在切换前 flush 所有 dirty 文件', async () => {
    const ref = createRef<WorkspaceFilesPanelHandle>();
    render(<WorkspaceFilesPanel ref={ref} target={first} isOpen isExpanded={false} onToggleExpanded={vi.fn()} onClose={vi.fn()} />);
    fireEvent.click(screen.getByRole('button', { name: 'mark dirty' }));
    expect(ref.current?.owner).toEqual({ scope: 'workspace', id: 'persistent', epoch: 0 });
    expect(ref.current?.hasDirty()).toBe(true);
    await expect(ref.current?.saveDirty()).resolves.toEqual({ ok: true, failedEntryIds: [] });
    expect(controls.save).toHaveBeenCalledTimes(1);
  });

  it('只保存发送附件点名的 dirty 文件', async () => {
    const ref = createRef<WorkspaceFilesPanelHandle>();
    const { rerender } = render(<WorkspaceFilesPanel ref={ref} target={first} isOpen isExpanded={false} onToggleExpanded={vi.fn()} onClose={vi.fn()} />);
    rerender(<WorkspaceFilesPanel ref={ref} target={second} isOpen isExpanded={false} onToggleExpanded={vi.fn()} onClose={vi.fn()} />);
    fireEvent.click(screen.getAllByRole('button', { name: 'mark dirty' })[0]);

    await expect(ref.current?.saveEntries(['two'])).resolves.toEqual({ ok: true, failedEntryIds: [] });

    expect(controls.save).toHaveBeenCalledTimes(1);
    expect(controls.save).toHaveBeenCalledWith('two.xlsx');
  });

  it('flush 在首个 await 前抓取全部 dirty 标签，随后卸载也不漏保存并聚合失败', async () => {
    const firstSave = deferred<{ ok: boolean; stale: boolean }>();
    controls.save.mockImplementation((path: string) => (
      path === first.path
        ? firstSave.promise
        : Promise.reject(new Error('second save failed'))
    ));
    const ref = createRef<WorkspaceFilesPanelHandle>();
    const { rerender, unmount } = render(<WorkspaceFilesPanel ref={ref} target={first} isOpen isExpanded={false} onToggleExpanded={vi.fn()} onClose={vi.fn()} />);
    rerender(<WorkspaceFilesPanel ref={ref} target={second} isOpen isExpanded={false} onToggleExpanded={vi.fn()} onClose={vi.fn()} />);
    fireEvent.click(screen.getAllByRole('button', { name: 'mark dirty' })[0]);

    const saving = ref.current!.saveDirty();
    expect(controls.save.mock.calls.map(([path]) => path)).toEqual(['one.md', 'two.xlsx']);
    unmount();
    firstSave.resolve({ ok: true, stale: false });

    await expect(saving).resolves.toEqual({ ok: false, failedEntryIds: ['two'] });
  });

  it('删除 tombstone 同步关闭 dirty 标签，不先保存将被删除的草稿', async () => {
    const ref = createRef<WorkspaceFilesPanelHandle>();
    const { rerender } = render(<WorkspaceFilesPanel ref={ref} target={first} isOpen isExpanded={false} onToggleExpanded={vi.fn()} onClose={vi.fn()} />);
    rerender(<WorkspaceFilesPanel ref={ref} target={second} isOpen isExpanded={false} onToggleExpanded={vi.fn()} onClose={vi.fn()} />);
    fireEvent.click(screen.getAllByRole('button', { name: 'mark dirty' })[0]);

    api.get.mockClear();
    act(() => emitWorkspaceMutation({
      operation: 'delete',
      affectedEntryIds: ['one', 'two'],
      tombstone: true,
      origin: 'local',
    }));

    expect(screen.queryByRole('tab', { name: 'one.md' })).not.toBeInTheDocument();
    expect(screen.queryByRole('tab', { name: 'two.xlsx' })).not.toBeInTheDocument();
    expect(api.get).not.toHaveBeenCalled();
  });

  it('关闭 dirty 标签时先保存，成功后才卸载标签', async () => {
    const onClose = vi.fn();
    render(<WorkspaceFilesPanel target={first} isOpen isExpanded={false} onToggleExpanded={vi.fn()} onClose={onClose} />);
    fireEvent.click(screen.getByRole('button', { name: 'mark dirty' }));
    fireEvent.click(screen.getByRole('button', { name: '关闭 one.md' }));

    expect(controls.save).toHaveBeenCalledTimes(1);
    await waitFor(() => expect(screen.queryByRole('tab', { name: 'one.md' })).not.toBeInTheDocument());
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it('模型更新已打开文件时只原位刷新对应预览，不卸载面板或活动标签', async () => {
    const updated = {
      ...first,
      revision: 2,
      size_bytes: 18,
      updated_at: 'later',
      sha256: 'updated',
    };
    api.get.mockResolvedValue({ data: updated });
    render(<WorkspaceFilesPanel target={first} isOpen isExpanded={false} onToggleExpanded={vi.fn()} onClose={vi.fn()} />);
    const panel = screen.getByTestId('workspace-files-panel');
    const preview = screen.getByTestId('preview-one.md');
    const activeTab = screen.getByRole('tab', { name: 'one.md' });
    expect(preview).toHaveAttribute('data-refresh-in-place', 'true');

    act(() => emitWorkspaceInvalidation({ operation: 'updated', entryId: first.entry_id }));

    await waitFor(() => expect(preview).toHaveAttribute('data-revision', '2'));
    expect(screen.getByTestId('workspace-files-panel')).toBe(panel);
    expect(screen.getByTestId('preview-one.md')).toBe(preview);
    expect(screen.getByRole('tab', { name: 'one.md' })).toBe(activeTab);
    expect(activeTab).toHaveAttribute('aria-selected', 'true');
  });

  it('统一 invalidation 入口丢弃低于已打开 entry 的旧 revision，不发多余刷新', async () => {
    const newest = { ...first, entry_id: 'newest-open-entry', revision: 5 };
    render(<WorkspaceFilesPanel target={newest} isOpen isExpanded={false} onToggleExpanded={vi.fn()} onClose={vi.fn()} />);

    act(() => emitWorkspaceInvalidation({
      operation: 'updated',
      entryId: newest.entry_id,
      revision: 4,
    }));
    await act(async () => { await Promise.resolve(); });

    expect(api.get).not.toHaveBeenCalledWith('/workspace/entries/newest-open-entry');
    expect(screen.getByTestId('preview-one.md')).toHaveAttribute('data-revision', '5');
  });

  it('模型更新 dirty 文件时保留本地草稿和旧 revision，不用外部版本静默覆盖', async () => {
    api.get.mockResolvedValue({ data: { ...first, revision: 2, updated_at: 'later' } });
    render(<WorkspaceFilesPanel target={first} isOpen isExpanded={false} onToggleExpanded={vi.fn()} onClose={vi.fn()} />);
    const preview = screen.getByTestId('preview-one.md');
    fireEvent.click(screen.getByRole('button', { name: 'mark dirty' }));

    act(() => emitWorkspaceInvalidation({ operation: 'updated', entryId: first.entry_id }));

    await waitFor(() => expect(api.get).toHaveBeenCalledWith('/workspace/entries/one'));
    expect(preview).toHaveAttribute('data-revision', '1');
    expect(screen.getByRole('tab', { name: 'one.md' })).toHaveAttribute('aria-selected', 'true');
  });

  it('重命名只更新标签和路径，不把未保存草稿误报为新版本冲突', () => {
    const versioned = { ...first, current_version_id: 'version-1' };
    render(<WorkspaceFilesPanel target={versioned} isOpen isExpanded={false} onToggleExpanded={vi.fn()} onClose={vi.fn()} />);
    fireEvent.click(screen.getByRole('button', { name: 'mark dirty' }));

    act(() => emitWorkspaceMutation({
      operation: 'rename',
      entry: {
        ...versioned,
        name: 'renamed.md',
        path: 'renamed.md',
        revision: 2,
        updated_at: 'later',
      },
      origin: 'local',
    }));

    expect(screen.getByRole('tab', { name: 'renamed.md' })).toHaveAttribute('aria-selected', 'true');
    expect(screen.getByTestId('preview-renamed.md')).toHaveAttribute('data-revision', '2');
    expect(screen.queryByText(/file 已有新版本|文件已有新版本/)).not.toBeInTheDocument();
  });

  it('远端同步失败也立即关闭，不再要求用户决定是否放弃', async () => {
    controls.save.mockResolvedValue({ ok: false, stale: false });
    const onClose = vi.fn();
    render(<WorkspaceFilesPanel target={first} isOpen isExpanded={false} onToggleExpanded={vi.fn()} onClose={onClose} />);
    fireEvent.click(screen.getByRole('button', { name: 'mark dirty' }));
    fireEvent.click(screen.getByRole('button', { name: '关闭 one.md' }));
    await waitFor(() => expect(controls.save).toHaveBeenCalledTimes(1));
    expect(screen.queryByRole('tab', { name: 'one.md' })).not.toBeInTheDocument();
    expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument();
    expect(onClose).toHaveBeenCalledTimes(1);
  });
});
