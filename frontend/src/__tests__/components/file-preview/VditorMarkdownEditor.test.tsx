import { act, createRef } from 'react';
import { render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import {
  VditorMarkdownEditor,
  type VditorMarkdownEditorHandle,
} from '../../../components/file-preview/VditorMarkdownEditor';

interface MockVditorOptions {
  value: string;
  mode: string;
  cache: { enable: boolean };
  toolbar: unknown[];
  toolbarConfig: { hide: boolean };
  input: (value: string) => void;
  after: () => void;
}

const { instances, MockVditor } = vi.hoisted(() => {
  const mockInstances: Array<{
    options: MockVditorOptions;
    value: string;
    setValue: (value: string, clearStack?: boolean) => void;
    destroy: () => void;
  }> = [];

  class VditorDouble {
    options: MockVditorOptions;
    value: string;
    setValue: (value: string, clearStack?: boolean) => void;
    destroy: () => void;

    constructor(mount: HTMLElement, options: MockVditorOptions) {
      this.options = options;
      this.value = options.value;
      this.setValue = vi.fn((value: string) => {
        this.value = value;
      });
      this.destroy = vi.fn();
      const scrollSurface = document.createElement('div');
      scrollSurface.className = 'vditor-wysiwyg';
      const editable = document.createElement('pre');
      editable.className = 'vditor-reset';
      editable.contentEditable = 'true';
      const imageMatch = options.value.match(/!\[[^\]]*\]\(([^)]+)\)/);
      if (imageMatch) {
        const image = document.createElement('img');
        image.setAttribute('src', imageMatch[1]);
        editable.appendChild(image);
      }
      scrollSurface.appendChild(editable);
      mount.appendChild(scrollSurface);
      mockInstances.push(this);
      queueMicrotask(() => options.after());
    }

    getValue() {
      return this.value;
    }
  }

  return { instances: mockInstances, MockVditor: VditorDouble };
});

vi.mock('vditor', () => ({ default: MockVditor }));
vi.mock('vditor/dist/index.css', () => ({}));
vi.mock('vditor/dist/js/i18n/zh_CN.js', () => ({}));
vi.mock('vditor/dist/js/icons/ant.js', () => ({}));
vi.mock('vditor/dist/js/lute/lute.min.js', () => ({}));

vi.mock('../../../services/api', () => ({
  apiService: {
    getAuthHeaders: vi.fn(() => ({ Authorization: 'Bearer editor-test' })),
  },
}));

describe('VditorMarkdownEditor', () => {
  beforeEach(() => {
    instances.length = 0;
    vi.stubGlobal('fetch', vi.fn());
    Object.defineProperty(URL, 'createObjectURL', {
      configurable: true,
      value: vi.fn(() => 'blob:session-image'),
    });
    Object.defineProperty(URL, 'revokeObjectURL', {
      configurable: true,
      value: vi.fn(),
    });
  });

  afterEach(() => {
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
    document.getElementById('vditorLuteScript')?.remove();
    document.getElementById('vditorIconScript')?.remove();
  });

  it('初始化单一所见即所得编辑器并暴露可访问文本框', async () => {
    const onChange = vi.fn();
    const buildSessionFileUrl = vi.fn((path: string) => `/api/session-files/${path}`);
    const ref = createRef<VditorMarkdownEditorHandle>();

    const { container, rerender, unmount } = render(
      <VditorMarkdownEditor
        ref={ref}
        markdown="# 初始内容"
        onChange={onChange}
        filePath="reports/report.md"
        buildSessionFileUrl={buildSessionFileUrl}
        toolbarOpen={false}
      />,
    );

    expect(await screen.findByRole('textbox', { name: 'Markdown 所见即所得编辑器' })).toHaveAttribute('aria-multiline', 'true');
    expect(instances[0].options).toMatchObject({
      mode: 'wysiwyg',
      cache: { enable: false },
      toolbar: expect.arrayContaining(['headings', 'bold', 'table', 'undo', 'redo']),
      toolbarConfig: { hide: false },
    });

    act(() => instances[0].options.input('# 用户修改'));
    expect(onChange).toHaveBeenCalledWith('# 用户修改');

    rerender(
      <VditorMarkdownEditor
        ref={ref}
        markdown="# 服务端新版本"
        onChange={onChange}
        filePath="reports/report.md"
        buildSessionFileUrl={buildSessionFileUrl}
        toolbarOpen
      />,
    );
    expect(container.firstElementChild).toHaveAttribute('data-toolbar-open', 'true');
    await waitFor(() => expect(instances[0].setValue).toHaveBeenCalledWith('# 服务端新版本', true));
    expect(ref.current?.getMarkdown()).toBe('# 服务端新版本');

    unmount();
    expect(instances[0].destroy).toHaveBeenCalledOnce();
  });

  it('鉴权加载会话图片且保存时还原 Markdown 相对路径', async () => {
    const fetchMock = vi.mocked(fetch).mockResolvedValue({
      ok: true,
      blob: () => Promise.resolve(new Blob(['image'])),
    } as Response);
    const onChange = vi.fn();
    const ref = createRef<VditorMarkdownEditorHandle>();

    const { unmount } = render(
      <VditorMarkdownEditor
        ref={ref}
        markdown="![趋势图](./assets/chart.png)"
        onChange={onChange}
        filePath="reports/report.md"
        buildSessionFileUrl={(path) => `/api/session-files/${path}`}
        toolbarOpen={false}
      />,
    );

    await screen.findByRole('textbox', { name: 'Markdown 所见即所得编辑器' });
    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith('/api/session-files/reports/assets/chart.png', {
        headers: { Authorization: 'Bearer editor-test' },
      });
      expect(screen.getByRole('img')).toHaveAttribute('src', 'blob:session-image');
    });

    instances[0].value = '![趋势图](blob:session-image)';
    act(() => instances[0].options.input(instances[0].value));
    expect(onChange).toHaveBeenLastCalledWith('![趋势图](./assets/chart.png)');
    expect(ref.current?.getMarkdown()).toBe('![趋势图](./assets/chart.png)');

    unmount();
    expect(URL.revokeObjectURL).toHaveBeenCalledWith('blob:session-image');
  });
});
