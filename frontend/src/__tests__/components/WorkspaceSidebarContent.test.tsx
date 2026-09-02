import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const client = vi.hoisted(() => ({
  get: vi.fn(), post: vi.fn(), put: vi.fn(), patch: vi.fn(), delete: vi.fn(),
}));

vi.mock('../../services/api', () => ({
  apiService: { getAxiosClient: () => client, getAuthHeaders: () => ({}) },
}));

import { WorkspaceSidebarContent } from '../../components/workspace/WorkspaceSidebarContent';
import { WorkspaceApiError } from '../../services/workspaceApi';
import { emitWorkspaceMutation, resetWorkspaceEventsForTests } from '../../services/workspaceEvents';
import type { WorkspaceEntry } from '../../types/workspace';

const folder: WorkspaceEntry = {
  entry_id: 'dir-1', parent_id: null, name: '资料', kind: 'directory', path: '资料', size_bytes: 0,
  mime_type: null, sha256: null, revision: 1, status: 'active', created_at: 'now', updated_at: 'now',
};
const file: WorkspaceEntry = {
  entry_id: 'file-1', parent_id: null, name: 'report.md', kind: 'file', path: 'report.md', size_bytes: 20,
  mime_type: 'text/markdown', sha256: 'hash', revision: 2, status: 'active', created_at: 'now', updated_at: 'now',
};

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((resolvePromise) => { resolve = resolvePromise; });
  return { promise, resolve };
}

function pointerDrag(source: HTMLElement, target: Element, onActive?: () => void) {
  const hitTest = vi.mocked(document.elementFromPoint);
  hitTest.mockReturnValue(target);
  fireEvent.pointerDown(source, { button: 0, pointerId: 1, clientX: 10, clientY: 10 });
  fireEvent.pointerMove(source, { pointerId: 1, clientX: 24, clientY: 24 });
  onActive?.();
  fireEvent.pointerUp(source, { pointerId: 1, clientX: 24, clientY: 24 });
  hitTest.mockReturnValue(null);
}

describe('WorkspaceSidebarContent', () => {
  beforeEach(() => {
    resetWorkspaceEventsForTests();
    vi.clearAllMocks();
    Object.defineProperty(window, 'PointerEvent', {
      configurable: true,
      value: MouseEvent,
    });
    Object.defineProperty(document, 'elementFromPoint', {
      configurable: true,
      value: vi.fn(() => null),
    });
    client.get.mockImplementation(async (_url: string) => ({
      data: {
        items: [file, folder],
        next_cursor: null,
        workspace_revision: 1,
      },
    }));
  });

  it('目录优先排序，文件树支持 ArrowDown/Enter 并打开右侧工作台', async () => {
    const onOpen = vi.fn();
    render(<WorkspaceSidebarContent onOpenEntry={onOpen} />);

    const tree = await screen.findByRole('tree', { name: '工作区文件树' });
    expect(screen.getByTestId('workspace-sidebar-content')).toHaveClass(
      'min-w-0',
      'w-full',
    );
    expect(screen.getByTestId('workspace-content-body')).toHaveClass('min-w-0', 'w-full');
    expect(tree).toHaveClass('min-w-0', 'w-full');
    expect(tree.querySelectorAll('[role="treeitem"]')[0]).toHaveTextContent('资料');
    const fileRow = screen.getByTestId('workspace-drag-row-file-1');
    expect(fileRow).toHaveClass('mx-1', 'pl-1');
    expect(fileRow.firstElementChild).toHaveClass('w-5');
    expect(within(fileRow).getByRole('checkbox', { name: '选择 report.md' })).toBeInTheDocument();
    const fileButton = screen.getByRole('button', { name: 'report.md' });
    expect(fileRow.children[2]).toBe(fileButton);
    const folderButton = screen.getByRole('button', { name: '资料' });
    folderButton.focus();
    fireEvent.keyDown(folderButton, { key: 'ArrowDown' });
    expect(screen.getByRole('button', { name: 'report.md' })).toHaveFocus();
    fireEvent.keyDown(screen.getByRole('button', { name: 'report.md' }), { key: 'Enter' });
    expect(onOpen).toHaveBeenCalledWith(file);
  });

  it('同层条目共用展开槽，文件夹子项向右缩进一个清晰层级', async () => {
    const nestedFile = {
      ...file,
      entry_id: 'nested-file',
      parent_id: 'dir-1',
      path: '资料/report.md',
    };
    client.get.mockImplementation(async (_url: string, config?: { params?: Record<string, unknown> }) => ({
      data: {
        items: config?.params?.parent_id === 'dir-1' ? [nestedFile] : [folder, file],
        next_cursor: null,
        workspace_revision: 1,
      },
    }));
    render(<WorkspaceSidebarContent onOpenEntry={vi.fn()} />);

    const rootFolderRow = await screen.findByTestId('workspace-drag-row-dir-1');
    const rootFileRow = screen.getByTestId('workspace-drag-row-file-1');
    expect(rootFolderRow).toHaveClass('mx-1', 'pl-1');
    expect(rootFileRow).toHaveClass('mx-1', 'pl-1');

    fireEvent.click(screen.getByRole('button', { name: '展开 资料' }));
    const nestedFileRow = await screen.findByTestId('workspace-drag-row-nested-file');
    const nestedGroup = screen.getByRole('group');
    expect(nestedGroup).toHaveClass('ml-2');
    expect(nestedGroup).not.toHaveClass('border-l', 'pl-1');
    expect(nestedGroup).toContainElement(nestedFileRow);
    expect(nestedFileRow).toHaveClass('mx-1', 'pl-1');
    expect(nestedFileRow.firstElementChild).toHaveClass('w-5');
  });

  it('根目录到第三级的长名称均可悬停查看全名，且只收缩文本不收缩格式图标', async () => {
    const rootFile = {
      ...file,
      name: '长江存储产业链深度研究报告.md',
      path: '长江存储产业链深度研究报告.md',
    };
    const secondLevelFolder: WorkspaceEntry = {
      ...folder,
      entry_id: 'dir-2',
      parent_id: 'dir-1',
      name: 'luna-e2e-20260828-二级超长文件夹名称',
      path: '资料/luna-e2e-20260828-二级超长文件夹名称',
    };
    const thirdLevelFile = {
      ...file,
      entry_id: 'third-level-long-file',
      parent_id: 'dir-2',
      name: 'luna-e2e-20260828-第三级超长回归文件名.json',
      path: '资料/luna-e2e-20260828-二级超长文件夹名称/luna-e2e-20260828-第三级超长回归文件名.json',
      mime_type: 'application/json',
    };
    client.get.mockImplementation(async (_url: string, config?: { params?: Record<string, unknown> }) => ({
      data: {
        items: config?.params?.parent_id === 'dir-1'
          ? [secondLevelFolder]
          : config?.params?.parent_id === 'dir-2'
            ? [thirdLevelFile]
            : [folder, rootFile],
        next_cursor: null,
        workspace_revision: 1,
      },
    }));
    render(<WorkspaceSidebarContent onOpenEntry={vi.fn()} />);

    const rootButton = await screen.findByRole('button', { name: rootFile.name });
    expect(rootButton).toHaveAttribute('title', rootFile.name);
    expect(rootButton.parentElement?.querySelector('svg')).toHaveAttribute('width', '15');
    expect(rootButton.parentElement?.querySelector('svg')).toHaveClass('lucide-file-text', 'shrink-0');
    expect(screen.getByText(rootFile.name)).toHaveClass('min-w-0', 'flex-1', 'truncate');

    fireEvent.click(screen.getByRole('button', { name: '展开 资料' }));
    const secondLevelButton = await screen.findByRole('button', { name: secondLevelFolder.name });
    expect(secondLevelButton).toHaveAttribute('title', secondLevelFolder.name);
    fireEvent.click(screen.getByRole('button', { name: `展开 ${secondLevelFolder.name}` }));

    const thirdLevelButton = await screen.findByRole('button', { name: thirdLevelFile.name });
    expect(thirdLevelButton).toHaveAttribute('title', thirdLevelFile.name);
    expect(thirdLevelButton.parentElement?.querySelector('svg')).toHaveAttribute('width', '15');
    expect(thirdLevelButton.parentElement?.querySelector('svg')).toHaveClass('lucide-file-code', 'shrink-0');
    expect(screen.getByText(thirdLevelFile.name)).toHaveClass('min-w-0', 'flex-1', 'truncate');
  });

  it('顶部操作菜单没有回收站，支持 Escape、新建与打开文件', async () => {
    const onOpen = vi.fn();
    client.post.mockResolvedValueOnce({
      data: { status: 'CREATED', entry: { ...file, entry_id: 'new-file', name: '未命名.md', path: '未命名.md' }, mutation_id: 'm1' },
    });
    render(<WorkspaceSidebarContent onOpenEntry={onOpen} />);
    await screen.findByRole('button', { name: 'report.md' });

    const menuButton = screen.getByRole('button', { name: '工作区操作' });
    const menuRectSpy = vi.spyOn(menuButton, 'getBoundingClientRect').mockReturnValue({
      x: 200, y: 100, left: 200, right: 232, top: 100, bottom: 132,
      width: 32, height: 32, toJSON: () => ({}),
    });
    fireEvent.click(menuButton);
    expect(screen.getByText('新建 Markdown')).toBeInTheDocument();
    const portalMenu = screen.getByRole('menu', { name: '工作区根目录操作菜单' });
    expect(within(portalMenu).queryByText('回收站')).not.toBeInTheDocument();
    expect(portalMenu.parentElement).toBe(document.body);
    expect(portalMenu).toHaveClass('fixed', 'w-[172px]');
    expect(portalMenu).toHaveStyle({ left: '240px', top: '100px' });
    expect(screen.getByRole('menuitem', { name: '新建 Markdown' })).toHaveClass('h-9', 'text-[12px]');
    fireEvent.keyDown(document, { key: 'Escape' });
    expect(screen.queryByText('新建 Markdown')).not.toBeInTheDocument();

    menuRectSpy.mockReturnValue({
      x: 980, y: 100, left: 980, right: 1012, top: 100, bottom: 132,
      width: 32, height: 32, toJSON: () => ({}),
    });
    fireEvent.click(menuButton);
    expect(screen.getByRole('menu', { name: '工作区根目录操作菜单' })).toHaveStyle({ left: '800px' });
    fireEvent(window, new Event('resize'));
    expect(screen.queryByRole('menu', { name: '工作区根目录操作菜单' })).not.toBeInTheDocument();

    fireEvent.click(menuButton);
    fireEvent.click(screen.getByText('新建 Markdown'));
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    await waitFor(() => expect(client.post).toHaveBeenCalledWith('/workspace/files', expect.objectContaining({ parent_id: null, name: '未命名.md', file_type: 'markdown' })));
    expect(onOpen).toHaveBeenCalledWith(expect.objectContaining({ entry_id: 'new-file' }));

    const rowMenuButton = screen.getByRole('button', { name: 'report.md 操作' });
    expect(rowMenuButton).toHaveClass('mr-3');
    vi.spyOn(rowMenuButton, 'getBoundingClientRect').mockReturnValue({
      x: 200, y: 180, left: 200, right: 232, top: 180, bottom: 212,
      width: 32, height: 32, toJSON: () => ({}),
    });
    fireEvent.click(rowMenuButton);
    const rowMenu = screen.getByRole('menu', { name: 'report.md 操作菜单' });
    expect(rowMenu.parentElement).toBe(document.body);
    expect(rowMenu).toHaveClass('fixed', 'w-[140px]');
    expect(rowMenu).toHaveStyle({ left: '240px', top: '180px' });
    expect(within(rowMenu).getAllByRole('menuitem').map((item) => item.textContent)).toEqual(['重命名', '删除']);
    fireEvent.keyDown(document, { key: 'Escape' });
    expect(screen.queryByRole('menu', { name: 'report.md 操作菜单' })).not.toBeInTheDocument();
    expect(rowMenuButton).toHaveFocus();
  });

  it('拖拽文件到目录完成移动，行菜单不重复提供移动入口', async () => {
    client.get.mockImplementation(async (_url: string, config?: { params?: Record<string, unknown> }) => ({
      data: {
        items: config?.params?.parent_id === 'dir-1' ? [file] : [file, folder],
        next_cursor: null,
        workspace_revision: 1,
      },
    }));
    client.patch.mockResolvedValueOnce({
      data: { status: 'MOVED', entry: { ...file, parent_id: 'dir-1', path: '资料/report.md', revision: 3 }, mutation_id: 'm2' },
    });
    render(<WorkspaceSidebarContent onOpenEntry={vi.fn()} />);
    await screen.findByRole('button', { name: 'report.md' });

    const source = screen.getByRole('button', { name: 'report.md' });
    const target = screen.getByTestId('workspace-drag-row-dir-1');
    expect(screen.getByTestId('workspace-drag-row-file-1')).not.toHaveAttribute('draggable');
    expect(source.tagName).toBe('DIV');
    expect(source).toHaveAttribute('data-workspace-pointer-drag-source');
    expect(source).not.toHaveAttribute('draggable');
    pointerDrag(source, target, () => {
      expect(source).toHaveAttribute('aria-grabbed', 'true');
      expect(target).toHaveClass('ring-claude-accent/35');
      const preview = screen.getByTestId('workspace-drag-preview');
      expect(preview).toHaveTextContent('report.md');
      expect(preview).toHaveClass('pointer-events-none', 'fixed', 'shadow-[0_8px_22px_rgba(30,26,20,0.18)]');
      expect(preview.querySelector('svg')).toHaveClass('lucide-file-text');
      expect(preview).toHaveStyle({ transform: 'translate3d(38px, 38px, 0)' });
    });
    expect(screen.queryByTestId('workspace-drag-preview')).not.toBeInTheDocument();

    await waitFor(() => expect(client.patch).toHaveBeenCalledWith('/workspace/entries/file-1', expect.objectContaining({ parent_id: 'dir-1', expected_revision: 2 })));
    fireEvent.click(screen.getByRole('button', { name: 'report.md 操作' }));
    expect(screen.queryByText('移动')).not.toBeInTheDocument();
  });

  it('拖到目录内文件行或子列表空白时都移动到该父目录', async () => {
    const secondSource: WorkspaceEntry = {
      ...file,
      entry_id: 'file-2',
      name: 'summary.md',
      path: 'summary.md',
      sha256: 'hash-2',
    };
    const existingChild: WorkspaceEntry = {
      ...file,
      entry_id: 'existing-child',
      parent_id: 'dir-1',
      name: 'existing.md',
      path: '资料/existing.md',
      sha256: 'existing-hash',
    };
    client.get.mockImplementation(async (_url: string, config?: { params?: Record<string, unknown> }) => ({
      data: {
        items: config?.params?.parent_id === 'dir-1' ? [existingChild] : [folder, file, secondSource],
        next_cursor: null,
        workspace_revision: 1,
      },
    }));
    client.patch
      .mockResolvedValueOnce({
        data: { status: 'MOVED', entry: { ...file, parent_id: 'dir-1', path: '资料/report.md', revision: 3 }, mutation_id: 'move-on-file' },
      })
      .mockResolvedValueOnce({
        data: { status: 'MOVED', entry: { ...secondSource, parent_id: 'dir-1', path: '资料/summary.md', revision: 3 }, mutation_id: 'move-on-gap' },
      });
    render(<WorkspaceSidebarContent onOpenEntry={vi.fn()} />);
    fireEvent.click(await screen.findByRole('button', { name: '展开 资料' }));

    const folderRow = screen.getByTestId('workspace-drag-row-dir-1');
    const existingChildRow = await screen.findByTestId('workspace-drag-row-existing-child');
    expect(existingChildRow).toHaveAttribute('data-workspace-drop-target', 'dir-1');
    pointerDrag(screen.getByRole('button', { name: 'report.md' }), existingChildRow, () => {
      expect(folderRow).toHaveClass('ring-claude-accent/35');
    });
    await waitFor(() => expect(client.patch).toHaveBeenNthCalledWith(1, '/workspace/entries/file-1', expect.objectContaining({
      parent_id: 'dir-1',
      expected_revision: 2,
    })));

    const childGroup = screen.getByRole('group');
    expect(childGroup).toHaveAttribute('data-workspace-drop-target', 'dir-1');
    pointerDrag(screen.getByRole('button', { name: 'summary.md' }), childGroup, () => {
      expect(childGroup).toHaveClass('bg-claude-accent/[0.035]');
    });
    await waitFor(() => expect(client.patch).toHaveBeenNthCalledWith(2, '/workspace/entries/file-2', expect.objectContaining({
      parent_id: 'dir-1',
      expected_revision: 2,
    })));
  });

  it('拖回当前父目录不会冒泡成移到根目录，只有显式根目录区域才执行移动', async () => {
    const nestedFile = { ...file, parent_id: 'dir-1', path: '资料/report.md' };
    client.get.mockImplementation(async (_url: string, config?: { params?: Record<string, unknown> }) => ({
      data: {
        items: config?.params?.parent_id === 'dir-1' ? [nestedFile] : [folder],
        next_cursor: null,
        workspace_revision: 1,
      },
    }));
    client.patch.mockResolvedValueOnce({
      data: { status: 'MOVED', entry: { ...nestedFile, parent_id: null, path: 'report.md', revision: 3 }, mutation_id: 'move-root' },
    });
    render(<WorkspaceSidebarContent onOpenEntry={vi.fn()} />);
    fireEvent.click(await screen.findByRole('button', { name: '展开 资料' }));

    const source = await screen.findByRole('button', { name: 'report.md' });
    const currentParent = screen.getByTestId('workspace-drag-row-dir-1');
    pointerDrag(source, currentParent);
    expect(client.patch).not.toHaveBeenCalled();

    const rootSource = screen.getByRole('button', { name: 'report.md' });
    const hitTest = vi.mocked(document.elementFromPoint);
    hitTest.mockReturnValue(rootSource);
    fireEvent.pointerDown(rootSource, { button: 0, pointerId: 2, clientX: 10, clientY: 10 });
    fireEvent.pointerMove(rootSource, { pointerId: 2, clientX: 24, clientY: 24 });
    const rootDropZone = screen.getByTestId('workspace-root-drop-zone');
    hitTest.mockReturnValue(rootDropZone);
    fireEvent.pointerMove(rootSource, { pointerId: 2, clientX: 30, clientY: 30 });
    expect(rootDropZone).toHaveClass('border-claude-accent');
    fireEvent.pointerUp(rootSource, { pointerId: 2, clientX: 30, clientY: 30 });
    hitTest.mockReturnValue(null);

    await waitFor(() => expect(client.patch).toHaveBeenCalledWith('/workspace/entries/file-1', expect.objectContaining({
      parent_id: null,
      expected_revision: 2,
    })));
  });

  it('同一文件连续拖拽三次时逐次使用服务端返回的新 revision', async () => {
    const otherFolder: WorkspaceEntry = {
      ...folder,
      entry_id: 'dir-other',
      name: '归档',
      path: '归档',
    };
    const nestedFile: WorkspaceEntry = { ...file, parent_id: 'dir-1', path: '资料/report.md' };
    client.get.mockImplementation(async (_url: string, config?: { params?: Record<string, unknown> }) => ({
      data: {
        items: config?.params?.parent_id === 'dir-1'
          ? [nestedFile]
          : config?.params?.parent_id === 'dir-other'
            ? []
            : [folder, otherFolder],
        next_cursor: null,
        workspace_revision: 1,
      },
    }));
    client.patch
      .mockResolvedValueOnce({
        data: { status: 'MOVED', entry: { ...nestedFile, parent_id: null, path: 'report.md', revision: 3 }, mutation_id: 'move-1' },
      })
      .mockResolvedValueOnce({
        data: { status: 'MOVED', entry: { ...nestedFile, revision: 4 }, mutation_id: 'move-2' },
      })
      .mockResolvedValueOnce({
        data: { status: 'MOVED', entry: { ...nestedFile, parent_id: 'dir-other', path: '归档/report.md', revision: 5 }, mutation_id: 'move-3' },
      });
    render(<WorkspaceSidebarContent onOpenEntry={vi.fn()} />);
    fireEvent.click(await screen.findByRole('button', { name: '展开 资料' }));

    let source = await screen.findByRole('button', { name: 'report.md' });
    const hitTest = vi.mocked(document.elementFromPoint);
    hitTest.mockReturnValue(source);
    fireEvent.pointerDown(source, { button: 0, pointerId: 2, clientX: 10, clientY: 10 });
    fireEvent.pointerMove(source, { pointerId: 2, clientX: 24, clientY: 24 });
    const rootDropZone = screen.getByTestId('workspace-root-drop-zone');
    hitTest.mockReturnValue(rootDropZone);
    fireEvent.pointerMove(source, { pointerId: 2, clientX: 30, clientY: 30 });
    fireEvent.pointerUp(source, { pointerId: 2, clientX: 30, clientY: 30 });
    hitTest.mockReturnValue(null);
    await waitFor(() => expect(client.patch).toHaveBeenNthCalledWith(1, '/workspace/entries/file-1', expect.objectContaining({
      parent_id: null,
      expected_revision: 2,
    })));

    source = await screen.findByRole('button', { name: 'report.md' });
    await waitFor(() => expect(source).toHaveClass('cursor-grab'));
    const firstFolderTarget = screen.getByTestId('workspace-drag-row-dir-1');
    pointerDrag(source, firstFolderTarget);
    await waitFor(() => expect(client.patch).toHaveBeenNthCalledWith(2, '/workspace/entries/file-1', expect.objectContaining({
      parent_id: 'dir-1',
      expected_revision: 3,
    })));

    source = await screen.findByRole('button', { name: 'report.md' });
    await waitFor(() => expect(source).toHaveClass('cursor-grab'));
    const otherFolderTarget = screen.getByTestId('workspace-drag-row-dir-other');
    pointerDrag(source, otherFolderTarget);
    await waitFor(() => expect(client.patch).toHaveBeenNthCalledWith(3, '/workspace/entries/file-1', expect.objectContaining({
      parent_id: 'dir-other',
      expected_revision: 4,
    })));
  });

  it('第三级文件可直接拖到其他文件夹', async () => {
    const secondLevelFolder: WorkspaceEntry = {
      ...folder,
      entry_id: 'dir-2',
      parent_id: 'dir-1',
      name: '季度',
      path: '资料/季度',
    };
    const otherFolder: WorkspaceEntry = {
      ...folder,
      entry_id: 'dir-other',
      name: '归档',
      path: '归档',
    };
    const thirdLevelFile: WorkspaceEntry = {
      ...file,
      parent_id: 'dir-2',
      name: '三级报告.md',
      path: '资料/季度/三级报告.md',
    };
    client.get.mockImplementation(async (_url: string, config?: { params?: Record<string, unknown> }) => ({
      data: {
        items: config?.params?.parent_id === 'dir-1'
          ? [secondLevelFolder]
          : config?.params?.parent_id === 'dir-2'
            ? [thirdLevelFile]
            : config?.params?.parent_id === 'dir-other'
              ? []
              : [folder, otherFolder],
        next_cursor: null,
        workspace_revision: 1,
      },
    }));
    client.patch.mockResolvedValueOnce({
      data: {
        status: 'MOVED',
        entry: {
          ...thirdLevelFile,
          parent_id: 'dir-other',
          path: `归档/${thirdLevelFile.name}`,
          revision: 3,
        },
        mutation_id: 'move-by-drag',
      },
    });
    render(<WorkspaceSidebarContent onOpenEntry={vi.fn()} />);
    fireEvent.click(await screen.findByRole('button', { name: '展开 资料' }));
    fireEvent.click(await screen.findByRole('button', { name: '展开 季度' }));

    const source = await screen.findByRole('button', { name: '三级报告.md' });
    const destination = screen.getByTestId('workspace-drag-row-dir-other');
    pointerDrag(source, destination, () => expect(destination).toHaveClass('ring-claude-accent/35'));

    await waitFor(() => expect(client.patch).toHaveBeenCalledWith('/workspace/entries/file-1', expect.objectContaining({
      parent_id: 'dir-other',
      expected_revision: 2,
    })));
  });

  it('默认文件名已存在或服务端发生 409 竞争时继续使用自然序号', async () => {
    const unnamed = { ...file, entry_id: 'unnamed-1', name: '未命名.md', path: '未命名.md' };
    client.get.mockResolvedValue({ data: { items: [folder, file, unnamed], next_cursor: null, workspace_revision: 1 } });
    client.post
      .mockRejectedValueOnce(Object.assign(new Error('name exists'), {
        isAxiosError: true,
        response: { status: 409, data: { detail: { code: 'NAME_CONFLICT', message: '名称已存在' } } },
      }))
      .mockResolvedValueOnce({
        data: { status: 'CREATED', entry: { ...file, entry_id: 'unnamed-3', name: '未命名 3.md', path: '未命名 3.md' }, mutation_id: 'm3' },
      });
    const onOpen = vi.fn();
    render(<WorkspaceSidebarContent onOpenEntry={onOpen} />);
    await screen.findByRole('button', { name: '未命名.md' });

    fireEvent.click(screen.getByRole('button', { name: '工作区操作' }));
    fireEvent.click(screen.getByRole('menuitem', { name: '新建 Markdown' }));

    await waitFor(() => expect(client.post).toHaveBeenCalledTimes(2));
    expect(client.post).toHaveBeenNthCalledWith(1, '/workspace/files', expect.objectContaining({ name: '未命名 2.md' }));
    expect(client.post).toHaveBeenNthCalledWith(2, '/workspace/files', expect.objectContaining({ name: '未命名 3.md' }));
    expect(onOpen).toHaveBeenCalledWith(expect.objectContaining({ name: '未命名 3.md' }));
  });

  it('顶部新建固定根目录，文件夹菜单为自身目录提供创建和刷新操作', async () => {
    client.get.mockImplementation(async (_url: string, config?: { params?: Record<string, unknown> }) => ({
      data: {
        items: config?.params?.parent_id === 'dir-1' ? [] : [folder, file],
        next_cursor: null,
        workspace_revision: 1,
      },
    }));
    client.post
      .mockResolvedValueOnce({
        data: {
          status: 'CREATED',
          entry: { ...file, entry_id: 'root-sheet', name: '未命名.xlsx', path: '未命名.xlsx', mime_type: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet' },
          mutation_id: 'root-sheet-mutation',
        },
      })
      .mockResolvedValueOnce({
        data: {
          status: 'CREATED',
          entry: { ...file, entry_id: 'folder-sheet', parent_id: 'dir-1', name: '未命名.xlsx', path: '资料/未命名.xlsx', mime_type: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet' },
          mutation_id: 'folder-sheet-mutation',
        },
      });
    const onOpen = vi.fn();
    render(<WorkspaceSidebarContent onOpenEntry={onOpen} />);
    fireEvent.click(await screen.findByRole('button', { name: '资料' }));

    fireEvent.click(screen.getByRole('button', { name: '工作区操作' }));
    fireEvent.click(screen.getByRole('menuitem', { name: '新建表格' }));

    await waitFor(() => expect(client.post).toHaveBeenNthCalledWith(1, '/workspace/files', expect.objectContaining({
      parent_id: null, name: '未命名.xlsx', file_type: 'xlsx',
    })));
    expect(screen.getByText('顶部新建位置：工作区根目录')).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: '资料 操作' }));
    const folderMenu = screen.getByRole('menu', { name: '资料 操作菜单' });
    expect(folderMenu).toHaveClass('w-[172px]');
    expect(within(folderMenu).getAllByRole('menuitem').map((item) => item.textContent)).toEqual([
      '新建文件夹', '新建 Markdown', '新建表格', '上传文件', '刷新', '重命名', '删除',
    ]);
    fireEvent.click(within(folderMenu).getByRole('menuitem', { name: '新建表格' }));

    await waitFor(() => expect(client.post).toHaveBeenNthCalledWith(2, '/workspace/files', expect.objectContaining({
      parent_id: 'dir-1', name: '未命名.xlsx', file_type: 'xlsx',
    })));
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(onOpen).toHaveBeenNthCalledWith(1, expect.objectContaining({ entry_id: 'root-sheet' }));
    expect(onOpen).toHaveBeenNthCalledWith(2, expect.objectContaining({ entry_id: 'folder-sheet' }));

    fireEvent.click(screen.getByRole('button', { name: '资料 操作' }));
    fireEvent.click(within(screen.getByRole('menu', { name: '资料 操作菜单' })).getByRole('menuitem', { name: '刷新' }));
    await waitFor(() => expect(client.get).toHaveBeenCalledWith('/workspace/entries', {
      params: expect.objectContaining({ parent_id: 'dir-1' }),
    }));


  });

  it('二级文件夹菜单不再提供新建文件夹，但仍可新建和上传文件', async () => {
    const nestedFolder = {
      ...folder,
      entry_id: 'dir-2',
      parent_id: 'dir-1',
      name: '季度',
      path: '资料/季度',
    };
    client.get.mockImplementation(async (_url: string, config?: { params?: Record<string, unknown> }) => ({
      data: {
        items: config?.params?.parent_id === 'dir-1' ? [nestedFolder] : [folder],
        next_cursor: null,
        workspace_revision: 1,
      },
    }));
    render(<WorkspaceSidebarContent onOpenEntry={vi.fn()} />);

    fireEvent.click(await screen.findByRole('button', { name: '展开 资料' }));
    fireEvent.click(await screen.findByRole('button', { name: '季度 操作' }));
    const nestedFolderMenu = screen.getByRole('menu', { name: '季度 操作菜单' });
    const itemLabels = within(nestedFolderMenu).getAllByRole('menuitem').map((item) => item.textContent);
    expect(itemLabels).toEqual(['新建 Markdown', '新建表格', '上传文件', '刷新', '重命名', '删除']);
    expect(within(nestedFolderMenu).queryByRole('menuitem', { name: '新建文件夹' })).not.toBeInTheDocument();
  });

  it('直接创建进行中禁用菜单项并阻止重复提交', async () => {
    let resolveCreate!: (value: unknown) => void;
    client.post.mockImplementationOnce(() => new Promise((resolve) => { resolveCreate = resolve; }));
    const onOpen = vi.fn();
    render(<WorkspaceSidebarContent onOpenEntry={onOpen} />);
    const menuButton = await screen.findByRole('button', { name: '工作区操作' });
    fireEvent.click(menuButton);
    fireEvent.click(screen.getByRole('menuitem', { name: '新建 Markdown' }));

    fireEvent.click(menuButton);
    const busyCreate = screen.getByRole('menuitem', { name: '新建 Markdown' });
    expect(busyCreate).toBeDisabled();
    fireEvent.click(busyCreate);
    expect(client.post).toHaveBeenCalledTimes(1);

    await act(async () => {
      resolveCreate({
        data: { status: 'CREATED', entry: { ...file, entry_id: 'busy-file', name: '未命名.md', path: '未命名.md' }, mutation_id: 'busy-mutation' },
      });
    });
    await waitFor(() => expect(onOpen).toHaveBeenCalledWith(expect.objectContaining({ entry_id: 'busy-file' })));
  });


  it('删除遇到 revision 冲突时刷新弹窗条目，再次确认使用最新 revision', async () => {
    const refreshedFile = { ...file, revision: 3, updated_at: 'later' };
    client.post
      .mockRejectedValueOnce(new WorkspaceApiError(409, {
        code: 'REVISION_CONFLICT',
        message: '条目已被其他操作修改',
        entry: refreshedFile,
      }))
      .mockResolvedValueOnce({
        data: {
          status: 'DELETED',
          mutation_id: 'delete-latest',
          affected_entry_ids: ['file-1'],
          root_count: 1,
          entry_count: 1,
        },
      });
    render(<WorkspaceSidebarContent onOpenEntry={vi.fn()} />);

    fireEvent.click(await screen.findByRole('button', { name: 'report.md 操作' }));
    fireEvent.click(screen.getByRole('menuitem', { name: '删除' }));
    const deleteDialog = screen.getByRole('alertdialog', { name: '删除“report.md”？' });
    fireEvent.click(within(deleteDialog).getByRole('button', { name: '删除' }));

    expect(await screen.findByRole('alert')).toHaveTextContent('所选条目刚刚更新，已保留选择，请再次确认。');
    const firstIntentKey = client.post.mock.calls[0][1].idempotency_key;
    fireEvent.click(within(deleteDialog).getByRole('button', { name: '删除' }));
    await waitFor(() => expect(client.post).toHaveBeenNthCalledWith(2, '/workspace/entries/delete-batch', {
      items: [{ entry_id: 'file-1', expected_revision: 3 }],
      idempotency_key: firstIntentKey,
    }));
    expect(screen.queryByRole('alertdialog', { name: '删除“report.md”？' })).not.toBeInTheDocument();
  });

  it('多选与键盘选择不改变名称点击语义，使用权威 revision 单次批量删除', async () => {
    const onOpen = vi.fn();

    client.post.mockResolvedValueOnce({
      data: {
        status: 'DELETED',
        mutation_id: 'batch-1',
        affected_entry_ids: ['dir-1', 'file-1'],
        root_count: 2,
        entry_count: 2,
      },
    });
    render(<WorkspaceSidebarContent onOpenEntry={onOpen} />);

    const folderButton = await screen.findByRole('button', { name: '资料' });
    const fileButton = screen.getByRole('button', { name: 'report.md' });
    fireEvent.keyDown(folderButton, { key: ' ' });
    fireEvent.keyDown(fileButton, { key: ' ', shiftKey: true });
    expect(screen.getByText('已选 2')).toBeInTheDocument();
    expect(folderButton).toHaveAttribute('aria-selected', 'true');
    expect(fileButton).toHaveAttribute('aria-selected', 'true');
    fireEvent.keyDown(fileButton, { key: 'Escape' });
    expect(screen.queryByText('已选 2')).not.toBeInTheDocument();

    fireEvent.keyDown(fileButton, { key: 'a', ctrlKey: true });
    fireEvent.click(screen.getByRole('button', { name: '删除' }));
    const dialog = screen.getByRole('alertdialog', { name: '将所选 2 项删除？' });
    fireEvent.click(within(dialog).getByRole('button', { name: '删除' }));

    expect(client.post).toHaveBeenCalledWith('/workspace/entries/delete-batch', {
      items: [
        { entry_id: 'dir-1', expected_revision: 1 },
        { entry_id: 'file-1', expected_revision: 2 },
      ],
      idempotency_key: expect.stringMatching(/^delete-batch:/),
    });
    await waitFor(() => {
      expect(screen.queryByRole('button', { name: '资料' })).not.toBeInTheDocument();
      expect(screen.queryByRole('button', { name: 'report.md' })).not.toBeInTheDocument();
    });
    expect(screen.queryByLabelText('关闭操作结果')).not.toBeInTheDocument();
    expect(onOpen).not.toHaveBeenCalled();
  });

  it('多选后移动到未加载目录，立即批量删除仍使用 authoritative revision 与 path', async () => {
    const archive: WorkspaceEntry = { ...folder, entry_id: 'archive-dir', name: '归档', path: '归档' };
    const second: WorkspaceEntry = { ...file, entry_id: 'file-2', name: 'second.md', path: 'second.md' };
    const third: WorkspaceEntry = { ...file, entry_id: 'file-3', name: 'third.md', path: 'third.md' };
    const movedSecond: WorkspaceEntry = {
      ...second,
      parent_id: archive.entry_id,
      path: '归档/second.md',
      revision: second.revision + 1,
    };
    const archiveLoad = deferred<{ data: { items: WorkspaceEntry[]; next_cursor: null; workspace_revision: number } }>();
    client.get.mockImplementation(async (_url: string, config?: { params?: Record<string, unknown> }) => (
      config?.params?.parent_id === archive.entry_id
        ? archiveLoad.promise
        : { data: { items: [archive, file, second, third], next_cursor: null, workspace_revision: 1 } }
    ));
    client.patch.mockResolvedValueOnce({
      data: { status: 'MOVED', entry: movedSecond, mutation_id: 'move-second' },
    });

    client.post.mockResolvedValueOnce({
      data: {
        status: 'DELETED', mutation_id: 'batch-after-move',
        affected_entry_ids: [file.entry_id, second.entry_id, third.entry_id],
        root_count: 3, entry_count: 3,
      },
    });
    render(<WorkspaceSidebarContent onOpenEntry={vi.fn()} />);

    for (const name of ['report.md', 'second.md', 'third.md']) {
      fireEvent.click(await screen.findByRole('checkbox', { name: `选择 ${name}` }));
    }
    pointerDrag(
      screen.getByRole('button', { name: 'second.md' }),
      screen.getByTestId('workspace-drag-row-archive-dir'),
    );
    await waitFor(() => expect(screen.queryByRole('button', { name: 'second.md' })).not.toBeInTheDocument());
    expect(client.get).toHaveBeenCalledWith('/workspace/entries', {
      params: expect.objectContaining({ parent_id: archive.entry_id }),
    });

    fireEvent.click(screen.getByRole('button', { name: '删除' }));
    fireEvent.click(within(screen.getByRole('alertdialog', { name: '将所选 3 项删除？' })).getByRole('button', { name: '删除' }));

    expect(client.post).toHaveBeenCalledWith('/workspace/entries/delete-batch', {
      items: expect.arrayContaining([
        { entry_id: file.entry_id, expected_revision: 2 },
        { entry_id: second.entry_id, expected_revision: 3 },
        { entry_id: third.entry_id, expected_revision: 2 },
      ]),
      idempotency_key: expect.stringMatching(/^delete-batch:/),
    });
    expect(screen.queryByText(/再次确认/)).not.toBeInTheDocument();
  });

  it('authoritative mutation 与快速确认同调用栈发生时不复用 render 闭包旧 revision', async () => {
    const moved = { ...file, parent_id: 'unloaded-dir', path: '未加载/report.md', revision: 3 };

    client.post.mockResolvedValueOnce({
      data: {
        status: 'DELETED', mutation_id: 'fast-batch',
        affected_entry_ids: [file.entry_id], root_count: 1, entry_count: 1,
      },
    });
    render(<WorkspaceSidebarContent onOpenEntry={vi.fn()} />);
    fireEvent.click(await screen.findByRole('checkbox', { name: '选择 report.md' }));
    const batchButton = screen.getByRole('button', { name: '删除' });

    act(() => {
      emitWorkspaceMutation({ operation: 'move', entry: moved, parentId: moved.parent_id });
      fireEvent.click(batchButton);
    });
    fireEvent.click(within(screen.getByRole('alertdialog', { name: '将所选 1 项删除？' })).getByRole('button', { name: '删除' }));

    expect(client.post).toHaveBeenCalledWith('/workspace/entries/delete-batch', {
      items: [{ entry_id: file.entry_id, expected_revision: 3 }],
      idempotency_key: expect.stringMatching(/^delete-batch:/),
    });
  });


  it('隐藏 anchor 后 Shift 从当前项重建单选，checkbox 焦点支持全选、Escape 与 Space', async () => {
    client.get.mockImplementation(async (_url: string, config?: { params?: Record<string, unknown> }) => ({
      data: {
        items: config?.params?.q ? [file] : [folder, file],
        next_cursor: null,
        workspace_revision: 1,
      },
    }));
    render(<WorkspaceSidebarContent onOpenEntry={vi.fn()} />);

    const folderCheckbox = await screen.findByRole('checkbox', { name: '选择 资料' });
    fireEvent.keyDown(folderCheckbox, { key: ' ' });
    expect(screen.getByText('已选 1')).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: '搜索工作区文件' }));
    fireEvent.change(screen.getByRole('textbox', { name: '搜索工作区' }), { target: { value: 'report' } });
    const resultCheckbox = await screen.findByRole('checkbox', { name: '选择 report.md' });
    fireEvent.keyDown(resultCheckbox, { key: ' ', shiftKey: true });
    expect(screen.getByText('已选 1')).toBeInTheDocument();
    expect(resultCheckbox).toBeChecked();
    expect(screen.getByRole('option', { name: 'report.md' })).toHaveAttribute('aria-selected', 'true');

    fireEvent.keyDown(resultCheckbox, { key: 'Escape' });
    expect(screen.queryByText('已选 1')).not.toBeInTheDocument();
    fireEvent.keyDown(resultCheckbox, { key: 'a', ctrlKey: true });
    expect(screen.getByText('已选 1')).toBeInTheDocument();
    fireEvent.keyDown(resultCheckbox, { key: 'Escape' });
    fireEvent.keyDown(resultCheckbox, { key: ' ' });
    expect(resultCheckbox).toBeChecked();
    expect(screen.getByText('已选 1')).toBeInTheDocument();
  });

  it('清空 query 立即失效延迟搜索，搜索专属选择仍从 authoritative cache 提交', async () => {
    const lateSearch = deferred<{ data: { items: WorkspaceEntry[]; next_cursor: null; workspace_revision: number } }>();
    const lateOnly = { ...file, entry_id: 'late-only', name: 'late.md', path: 'archive/late.md' };
    const searchOnly = { ...file, entry_id: 'search-only', name: 'unique.md', path: 'archive/unique.md', revision: 6 };
    client.get.mockImplementation(async (_url: string, config?: { params?: Record<string, unknown> }) => {
      if (config?.params?.q === 'late') return lateSearch.promise;
      return {
        data: {
          items: config?.params?.q === 'unique' ? [searchOnly] : [folder, file],
          next_cursor: null,
          workspace_revision: 1,
        },
      };
    });
    client.post.mockResolvedValueOnce({
      data: {
        status: 'DELETED', mutation_id: 'search-batch',
        affected_entry_ids: [searchOnly.entry_id], root_count: 1, entry_count: 1,
      },
    });
    render(<WorkspaceSidebarContent onOpenEntry={vi.fn()} />);
    await screen.findByRole('button', { name: 'report.md' });
    fireEvent.click(screen.getByRole('button', { name: '搜索工作区文件' }));
    const search = screen.getByRole('textbox', { name: '搜索工作区' });
    fireEvent.change(search, { target: { value: 'late' } });
    fireEvent.change(search, { target: { value: '' } });
    expect(document.querySelector('.animate-spin')).not.toBeInTheDocument();
    await act(async () => {
      lateSearch.resolve({ data: { items: [lateOnly], next_cursor: null, workspace_revision: 2 } });
      await Promise.resolve();
    });
    expect(screen.queryByText('archive/late.md')).not.toBeInTheDocument();

    fireEvent.change(search, { target: { value: 'unique' } });
    const checkbox = await screen.findByRole('checkbox', { name: '选择 unique.md' });
    fireEvent.click(checkbox);
    fireEvent.click(screen.getByRole('button', { name: '清空工作区搜索' }));
    expect(screen.getByText('已选 1')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '删除' }));
    fireEvent.click(within(screen.getByRole('alertdialog', { name: '将所选 1 项删除？' })).getByRole('button', { name: '删除' }));

    await waitFor(() => expect(client.post).toHaveBeenCalledWith('/workspace/entries/delete-batch', {
      items: [{ entry_id: searchOnly.entry_id, expected_revision: 6 }],
      idempotency_key: expect.stringMatching(/^delete-batch:/),
    }));
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  it('切到会话时失效在途搜索，返回工作区前旧结果不能继续参与批量操作', async () => {
    const refreshedSearch = deferred<{ data: { items: WorkspaceEntry[]; next_cursor: null; workspace_revision: number } }>();
    const staleResult = { ...file, entry_id: 'stale-search', name: 'stale.md', path: 'archive/stale.md', revision: 4 };
    const freshResult = { ...file, entry_id: 'fresh-search', name: 'fresh.md', path: 'archive/fresh.md', revision: 1 };
    let searchRequestCount = 0;
    client.get.mockImplementation(async (_url: string, config?: { params?: Record<string, unknown> }) => {
      if (config?.params?.q === 'archive') {
        searchRequestCount += 1;
        if (searchRequestCount === 1) {
          return { data: { items: [staleResult], next_cursor: null, workspace_revision: 1 } };
        }
        return refreshedSearch.promise;
      }
      return { data: { items: [folder, file], next_cursor: null, workspace_revision: 1 } };
    });

    const { rerender } = render(<WorkspaceSidebarContent isActive onOpenEntry={vi.fn()} />);
    await screen.findByRole('button', { name: 'report.md' });
    fireEvent.click(screen.getByRole('button', { name: '搜索工作区文件' }));
    fireEvent.change(screen.getByRole('textbox', { name: '搜索工作区' }), { target: { value: 'archive' } });
    fireEvent.click(await screen.findByRole('checkbox', { name: '选择 stale.md' }));

    rerender(<WorkspaceSidebarContent isActive={false} onOpenEntry={vi.fn()} />);
    expect(screen.queryByRole('option', { name: 'archive/stale.md' })).not.toBeInTheDocument();
    rerender(<WorkspaceSidebarContent isActive onOpenEntry={vi.fn()} />);

    expect(await screen.findByRole('status')).toHaveTextContent('正在刷新状态');
    expect(screen.getByRole('button', { name: '删除' })).toBeDisabled();
    expect(screen.queryByRole('option', { name: 'archive/stale.md' })).not.toBeInTheDocument();
    await waitFor(() => expect(searchRequestCount).toBe(2));
    await act(async () => {
      refreshedSearch.resolve({ data: { items: [freshResult], next_cursor: null, workspace_revision: 2 } });
      await Promise.resolve();
    });

    expect(await screen.findByRole('option', { name: 'archive/fresh.md' })).toBeInTheDocument();
    expect(screen.queryByRole('option', { name: 'archive/stale.md' })).not.toBeInTheDocument();
    expect(screen.getByRole('alert')).toHaveTextContent('1 项状态已失效');
    expect(screen.getByRole('button', { name: '删除' })).toBeDisabled();
  });


  it('只有文件夹保留紧凑命名 Dialog，支持 Escape、焦点恢复与准确标题', async () => {
    render(<WorkspaceSidebarContent onOpenEntry={vi.fn()} />);
    const menuButton = await screen.findByRole('button', { name: '工作区操作' });
    fireEvent.click(menuButton);
    fireEvent.click(screen.getByText('新建文件夹'));
    await waitFor(() => expect(screen.getByLabelText('名称')).toHaveFocus());
    const folderDialog = screen.getByRole('dialog', { name: '新建文件夹' });
    const folderNameInput = within(folderDialog).getByLabelText('名称');
    expect(folderDialog).toHaveClass('max-w-sm', 'rounded-xl', 'p-4');
    expect(folderNameInput).toHaveClass('h-10');
    expect(folderNameInput).toHaveValue('');
    expect(folderNameInput).toHaveAttribute('placeholder', '文件夹名称');
    expect(within(folderDialog).getByRole('button', { name: '取消' })).toHaveClass('h-10', 'text-[12px]');
    expect(within(folderDialog).getByRole('button', { name: '确定' })).toBeDisabled();
    fireEvent.keyDown(folderDialog, { key: 'Escape' });
    expect(screen.queryByRole('dialog', { name: '新建文件夹' })).not.toBeInTheDocument();
    await waitFor(() => expect(menuButton).toHaveFocus());

    fireEvent.click(screen.getByRole('button', { name: 'report.md 操作' }));
    fireEvent.click(screen.getByRole('menuitem', { name: '重命名' }));
    const renameDialog = screen.getByRole('dialog', { name: '重命名' });
    const renameInput = within(renameDialog).getByLabelText('名称') as HTMLInputElement;
    await waitFor(() => expect(renameInput).toHaveFocus());
    expect(renameInput).toHaveValue('report');
    expect(renameInput.selectionStart).toBe(0);
    expect(renameInput.selectionEnd).toBe('report'.length);
    expect(within(renameDialog).getByText('文件格式 .md 将保持不变')).toBeInTheDocument();

    client.patch.mockResolvedValueOnce({
      data: { status: 'UPDATED', entry: { ...file, name: '季度报告.md', path: '季度报告.md', revision: 3 }, mutation_id: 'rename-1' },
    });
    fireEvent.change(renameInput, { target: { value: '季度报告' } });
    fireEvent.click(within(renameDialog).getByRole('button', { name: '确定' }));
    await waitFor(() => expect(client.patch).toHaveBeenCalledWith('/workspace/entries/file-1', expect.objectContaining({
      name: '季度报告.md',
      expected_revision: 2,
    })));
  });

  it('直接新建失败时保留明确 inline error', async () => {
    client.post.mockRejectedValueOnce(new Error('创建文件失败'));
    render(<WorkspaceSidebarContent onOpenEntry={vi.fn()} />);
    await screen.findByRole('button', { name: '工作区操作' });
    fireEvent.click(screen.getByRole('button', { name: '工作区操作' }));
    fireEvent.click(screen.getByRole('menuitem', { name: '新建 Markdown' }));
    await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent('创建文件失败'));
  });

  it('工作区加载失败时结束等待并提供可恢复的重试入口', async () => {
    client.get
      .mockRejectedValueOnce(new Error('工作区服务暂不可用'))
      .mockResolvedValueOnce({ data: { items: [folder, file], next_cursor: null, workspace_revision: 1 } });
    render(<WorkspaceSidebarContent onOpenEntry={vi.fn()} />);

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('工作区服务暂不可用');
    expect(document.querySelector('.animate-spin')).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '重试加载工作区' }));

    expect(await screen.findByRole('button', { name: 'report.md' })).toBeInTheDocument();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  it('文件夹菜单上传文件时把该文件夹作为父目录', async () => {
    client.get.mockImplementation(async (_url: string, config?: { params?: Record<string, unknown> }) => ({
      data: {
        items: config?.params?.parent_id === 'dir-1' ? [] : [file, folder],
        next_cursor: null,
        workspace_revision: 1,
      },
    }));
    client.post.mockResolvedValueOnce({ data: { status: 'CREATED', entry: { ...file, entry_id: 'uploaded', parent_id: 'dir-1', name: 'data.csv', path: '资料/data.csv' }, mutation_id: 'upload-1' } });
    const { container } = render(<WorkspaceSidebarContent onOpenEntry={vi.fn()} />);
    fireEvent.click(await screen.findByRole('button', { name: '资料 操作' }));
    fireEvent.click(within(screen.getByRole('menu', { name: '资料 操作菜单' })).getByRole('menuitem', { name: '上传文件' }));
    const input = container.querySelector<HTMLInputElement>('input[type="file"]')!;
    const upload = new File(['a,b'], 'data.csv', { type: 'text/csv' });
    fireEvent.change(input, { target: { files: [upload] } });
    await waitFor(() => expect(client.post).toHaveBeenCalledWith(
      '/workspace/uploads',
      expect.any(FormData),
      { headers: { 'Content-Type': 'multipart/form-data' } },
    ));
    const body = client.post.mock.calls[0][1] as FormData;
    expect(body.get('parent_id')).toBe('dir-1');
    await waitFor(() => expect(client.get).toHaveBeenCalledWith('/workspace/entries', {
      params: expect.objectContaining({ parent_id: 'dir-1' }),
    }));
  });

  it('根目录通过 cursor 自动加载后续页', async () => {
    client.get.mockReset();
    client.get
      .mockResolvedValueOnce({ data: { items: [folder], next_cursor: 'next-page', workspace_revision: 1 } })
      .mockResolvedValueOnce({ data: { items: [file], next_cursor: null, workspace_revision: 1 } });
    render(<WorkspaceSidebarContent onOpenEntry={vi.fn()} />);
    expect(await screen.findByRole('button', { name: 'report.md' })).toBeInTheDocument();
    expect(client.get).toHaveBeenNthCalledWith(2, '/workspace/entries', {
      params: { limit: 200, cursor: 'next-page' },
    });
  });

  it('空工作区与搜索无结果都在剩余 panel 真正居中', async () => {
    client.get.mockResolvedValue({ data: { items: [], next_cursor: null, workspace_revision: 1 } });
    render(<WorkspaceSidebarContent onOpenEntry={vi.fn()} />);
    expect(await screen.findByText('工作区为空')).toBeInTheDocument();
    expect(screen.getByTestId('workspace-content-body')).toHaveClass('flex', 'min-h-0', 'flex-1', 'flex-col');
    expect(screen.getByTestId('workspace-empty-state')).toHaveClass('flex', 'min-h-0', 'flex-1', 'items-center', 'justify-center', 'text-center');
    expect(screen.getByText('可新建文件，或从会话文件面板存入内容')).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: '搜索工作区文件' }));
    const searchPopover = screen.getByRole('textbox', { name: '搜索工作区' }).closest('div');
    expect(searchPopover).toHaveClass('absolute');
    fireEvent.change(screen.getByRole('textbox', { name: '搜索工作区' }), { target: { value: 'missing' } });
    expect(await screen.findByText('没有匹配文件')).toBeInTheDocument();
    expect(screen.getByTestId('workspace-empty-state')).toHaveClass('items-center', 'justify-center', 'text-center');
  });
});
