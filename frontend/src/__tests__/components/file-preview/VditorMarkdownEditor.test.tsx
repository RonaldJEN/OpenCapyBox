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
        onOutlineChange={() => {}}
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
        onOutlineChange={() => {}}
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
        onOutlineChange={() => {}}
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
        onOutlineChange={() => {}}
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
        onOutlineChange={() => {}}
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
        onOutlineChange={() => {}}
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
        onOutlineChange={() => {}}
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
    render(<VditorMarkdownEditor markdown="正文" onChange={onChange} onOutlineChange={() => {}} filePath="report.md" buildSessionFileUrl={() => ''} toolbarOpen={false} />);
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
        onOutlineChange={() => {}}
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

  it('Lute 规范化不算修改：未编辑时对外给原文，真实修改才上报，改回原样回到原文，外部写入重建基线', async () => {
    const onChange = vi.fn();
    const ref = createRef<VditorMarkdownEditorHandle>();
    const source = '正文 $$x$$。\n\n概览\n---\n';
    const normalized = '正文 \n\n$$\nx\n$$\n\n。\n\n概览\n----\n';
    const buildUrl = (path: string) => path;
    const view = (markdown: string) => (
      <VditorMarkdownEditor
        ref={ref}
        markdown={markdown}
        onChange={onChange}
        onOutlineChange={() => {}}
        filePath="report.md"
        buildSessionFileUrl={buildUrl}
        toolbarOpen={false}
      />
    );
    const { rerender } = render(view(source));
    // 与 Lute 一样，画布读出的是重新序列化后的 Markdown，而不是原文。
    let canvas = normalized;
    (instances[0] as unknown as { getValue: () => string }).getValue = () => canvas;
    await screen.findByRole('textbox', { name: 'Markdown 所见即所得编辑器' });

    expect(ref.current!.getMarkdown()).toBe(source);
    act(() => instances[0].options.input(canvas));
    expect(onChange).not.toHaveBeenCalled();

    canvas = `${normalized}补充`;
    act(() => instances[0].options.input(canvas));
    expect(onChange).toHaveBeenLastCalledWith(canvas);
    expect(ref.current!.getMarkdown()).toBe(canvas);

    canvas = normalized;
    act(() => instances[0].options.input(canvas));
    expect(onChange).toHaveBeenLastCalledWith(source);
    expect(ref.current!.getMarkdown()).toBe(source);

    onChange.mockClear();
    canvas = '服务端新版本（已规范化）';
    rerender(view('服务端新版本'));
    expect(instances[0].setValue).toHaveBeenLastCalledWith('服务端新版本', true);
    expect(ref.current!.getMarkdown()).toBe('服务端新版本');
    act(() => instances[0].options.input(canvas));
    expect(onChange).not.toHaveBeenCalled();
  });

  it('同一次渲染里切换文件并换内容时，不向尚未就绪的新画布写入，新画布以最新内容建立基线', async () => {
    const onChange = vi.fn();
    const ref = createRef<VditorMarkdownEditorHandle>();
    const buildUrl = (path: string) => path;
    const view = (filePath: string, markdown: string) => (
      <VditorMarkdownEditor
        ref={ref}
        markdown={markdown}
        onChange={onChange}
        onOutlineChange={() => {}}
        filePath={filePath}
        buildSessionFileUrl={buildUrl}
        toolbarOpen={false}
      />
    );
    const { rerender } = render(view('a.md', '# A'));
    await screen.findByRole('textbox', { name: 'Markdown 所见即所得编辑器' });

    rerender(view('b.md', '# B'));
    expect(instances).toHaveLength(2);
    expect(instances[1].options.value).toBe('# B');
    expect(instances[1].setValue).not.toHaveBeenCalled();
    await waitFor(() => expect(ref.current!.getMarkdown()).toBe('# B'));
    expect(onChange).not.toHaveBeenCalled();
  });

  it('目录取自画布标题：内容变化后上报、按序号滚动到同一画布元素，销毁时清空', async () => {
    const onOutlineChange = vi.fn();
    const ref = createRef<VditorMarkdownEditorHandle>();
    const { unmount } = render(
      <VditorMarkdownEditor
        ref={ref}
        markdown="正文"
        onChange={() => {}}
        onOutlineChange={onOutlineChange}
        filePath="report.md"
        buildSessionFileUrl={(path) => path}
        toolbarOpen={false}
      />,
    );
    const editor = await screen.findByRole('textbox', { name: 'Markdown 所见即所得编辑器' });
    expect(onOutlineChange).toHaveBeenLastCalledWith([]);

    // Lute 把 YAML front matter 渲染为独立块；remark 会把同一段 `---` 之间的内容当成 setext 标题。
    editor.innerHTML = [
      '<div class="vditor-wysiwyg__block" data-type="yaml-front-matter"><pre><code>title: 报告</code></pre></div>',
      '<h1>报告</h1>',
      '<h2>收益 <span data-type="backslash"><span>\\</span>(</span>x^2<span data-type="backslash"><span>\\</span>)</span></h2>',
      '<h2>结论</h2>',
    ].join('');
    act(() => instances[0].options.input('# 报告'));
    expect(onOutlineChange).toHaveBeenLastCalledWith([
      { depth: 1, text: '报告' },
      { depth: 2, text: '收益 (x^2)' },
      { depth: 2, text: '结论' },
    ]);

    onOutlineChange.mockClear();
    act(() => instances[0].options.input('# 报告 '));
    expect(onOutlineChange).not.toHaveBeenCalled(); // 标题结构不变时不触发父组件渲染。

    const scrolled: Element[] = [];
    Element.prototype.scrollIntoView = vi.fn(function scrollIntoView(this: Element) { scrolled.push(this); });
    ref.current!.scrollToHeading(1);
    expect(scrolled).toEqual([editor.querySelectorAll('h2')[0]]);
    delete (Element.prototype as Partial<Element>).scrollIntoView;

    unmount();
    expect(onOutlineChange).toHaveBeenLastCalledWith([]);
  });

  it('销毁后 Vditor 延迟到达的 input 回调被忽略，不抛错也不上报', async () => {
    const onChange = vi.fn();
    const onOutlineChange = vi.fn();
    const { unmount } = render(
      <VditorMarkdownEditor
        markdown="# 正文"
        onChange={onChange}
        onOutlineChange={onOutlineChange}
        filePath="report.md"
        buildSessionFileUrl={(path) => path}
        toolbarOpen={false}
      />,
    );
    await screen.findByRole('textbox', { name: 'Markdown 所见即所得编辑器' });
    unmount();
    onOutlineChange.mockClear();

    expect(() => instances[0].options.input('# 正文改')).not.toThrow();
    expect(onChange).not.toHaveBeenCalled();
    expect(onOutlineChange).not.toHaveBeenCalled();
  });
});
