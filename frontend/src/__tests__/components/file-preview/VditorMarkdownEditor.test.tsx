import { act, createRef } from 'react';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
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

  it('始终初始化所见即所得画布，焦点不会暴露 Markdown 标记', async () => {
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
    await waitFor(() => expect(onChange).toHaveBeenCalledWith('# 用户修改'));

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

  it('在段落末尾连续按普通 Enter 会连续产生空段落', async () => {
    render(
      <VditorMarkdownEditor
        markdown="第一行"
        onChange={() => {}}
        filePath="report.md"
        buildSessionFileUrl={(path) => path}
        toolbarOpen={false}
      />,
    );
    const editor = await screen.findByRole('textbox', { name: 'Markdown 所见即所得编辑器' });
    editor.innerHTML = '<p data-block="0">第一行</p>';
    const paragraph = editor.querySelector('p')!;
    const range = document.createRange();
    range.selectNodeContents(paragraph);
    range.collapse(false);
    window.getSelection()?.removeAllRanges();
    window.getSelection()?.addRange(range);

    fireEvent.keyDown(editor, { key: 'Enter' });
    expect(editor.querySelectorAll(':scope > p')).toHaveLength(2);
    fireEvent.keyDown(editor, { key: 'Enter' });
    expect(editor.querySelectorAll(':scope > p')).toHaveLength(3);
    expect(Array.from(editor.querySelectorAll(':scope > p')).slice(1).every((paragraph) => (
      paragraph.textContent === '\u00a0'
      && paragraph.hasAttribute('data-opencapybox-empty-paragraph')
    ))).toBe(true);
  });

  it('自定义 Enter 用可 round-trip 的 HTML 语义投影空段，保存后重开仍保留段落', async () => {
    const onChange = vi.fn();
    const ref = createRef<VditorMarkdownEditorHandle>();
    const firstRender = render(
      <VditorMarkdownEditor
        ref={ref}
        markdown="第一行"
        onChange={onChange}
        filePath="report.md"
        buildSessionFileUrl={(path) => path}
        toolbarOpen={false}
      />,
    );
    const editor = await screen.findByRole('textbox', { name: 'Markdown 所见即所得编辑器' });
    editor.innerHTML = '<p data-block="0">第一行</p>';
    const paragraph = editor.querySelector('p')!;
    const range = document.createRange();
    range.selectNodeContents(paragraph);
    range.collapse(false);
    window.getSelection()?.removeAllRanges();
    window.getSelection()?.addRange(range);
    instances[0].value = '第一行\n\n';
    onChange.mockClear();

    fireEvent.keyDown(editor, { key: 'Enter' });

    expect(onChange).toHaveBeenCalledWith('第一行\n\n&nbsp;');
    expect(ref.current?.getMarkdown()).toBe('第一行\n\n&nbsp;');

    firstRender.unmount();
    render(
      <VditorMarkdownEditor
        markdown={'第一行\n\n&nbsp;'}
        onChange={() => {}}
        filePath="report.md"
        buildSessionFileUrl={(path) => path}
        toolbarOpen={false}
      />,
    );
    await screen.findByRole('textbox', { name: 'Markdown 所见即所得编辑器' });
    expect(instances[1].options.value).toBe('第一行\n\n&nbsp;');
  });

  it('输入不叠加编辑器内部 debounce，同一 tick 内即投影', async () => {
    const onChange = vi.fn();
    render(
      <VditorMarkdownEditor
        markdown="初稿"
        onChange={onChange}
        filePath="report.md"
        buildSessionFileUrl={(path) => path}
        toolbarOpen={false}
      />,
    );
    await screen.findByRole('textbox', { name: 'Markdown 所见即所得编辑器' });

    act(() => instances[0].options.input('初稿改'));

    expect(onChange).toHaveBeenCalledWith('初稿改');
  });

  it('只折叠纯 HTML 机器注释，不隐藏普通 HTML 块或改写 Markdown', async () => {
    const ref = createRef<VditorMarkdownEditorHandle>();
    const source = '<!-- ANCHOR:DAILY:START -->\n\n正文';
    render(
      <VditorMarkdownEditor
        ref={ref}
        markdown={source}
        onChange={() => {}}
        filePath="report.md"
        buildSessionFileUrl={(path) => path}
        toolbarOpen={false}
      />,
    );
    const editor = await screen.findByRole('textbox', { name: 'Markdown 所见即所得编辑器' });
    const commentBlock = document.createElement('div');
    commentBlock.className = 'vditor-wysiwyg__block';
    commentBlock.dataset.type = 'html-block';
    commentBlock.innerHTML = '<pre><code></code></pre><pre class="vditor-wysiwyg__preview"></pre>';
    commentBlock.querySelector('code')!.textContent = '<!-- ANCHOR:DAILY:START -->';
    const htmlBlock = commentBlock.cloneNode(true) as HTMLElement;
    htmlBlock.querySelector('code')!.textContent = '<section>可见内容</section>';
    editor.append(commentBlock, htmlBlock);

    await waitFor(() => expect(commentBlock).toHaveClass('file-preview-vditor-machine-comment'));
    expect(commentBlock).toHaveAttribute('aria-hidden', 'true');
    expect(htmlBlock).not.toHaveClass('file-preview-vditor-machine-comment');
    expect(htmlBlock).not.toHaveAttribute('aria-hidden');
    expect(ref.current?.getMarkdown()).toBe(source);
  });

  it('原生输入立即交付草稿，不等待 Vditor 延迟回调且不重复交付', async () => {
    const onChange = vi.fn();
    render(<VditorMarkdownEditor markdown="正文" onChange={onChange} filePath="report.md" buildSessionFileUrl={() => ''} toolbarOpen={false} />);
    const editor = await screen.findByRole('textbox', { name: 'Markdown 所见即所得编辑器' });
    instances[0].value = 'AAA正文';
    fireEvent.input(editor);
    expect(onChange).toHaveBeenCalledWith('AAA正文');
    act(() => instances[0].options.input('AAA正文'));
    expect(onChange).toHaveBeenCalledTimes(1);
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
    expect(onChange).not.toHaveBeenCalled(); // 图片 URL 转换没有改变正文，不应触发保存。
    expect(ref.current?.getMarkdown()).toBe('![趋势图](./assets/chart.png)');

    unmount();
    expect(URL.revokeObjectURL).toHaveBeenCalledWith('blob:session-image');
  });
});
