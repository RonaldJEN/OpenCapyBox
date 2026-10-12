import { describe, expect, it, vi } from 'vitest';
import { render } from '@testing-library/react';
import katex from 'katex';
import ReactMarkdown from 'react-markdown';
import { markdownMathComponents } from '../../components/MarkdownMath';
import { markdownMathRehypePlugins, markdownRemarkPlugins } from '../../utils/markdownMath';

const markdown = (content: string) => (
  <ReactMarkdown remarkPlugins={markdownRemarkPlugins} rehypePlugins={markdownMathRehypePlugins} components={markdownMathComponents}>{content}</ReactMarkdown>
);

function renderMarkdown(content: string) {
  return render(markdown(content)).container;
}

const tex = (root: ParentNode) => Array.from(root.querySelectorAll('.katex annotation')).map((node) => node.textContent!.trim());

describe('markdownMath 共享插件链', () => {
  it('独占段落的 \\[ \\] 渲染为独立公式块，含列表与引用缩进', () => {
    const container = renderMarkdown('\\[\ny=1\n\\]\n\n1. 公式：\n\n   \\[\n   \\frac{1}{2}\n   \\]\n\n> \\[\n> z\n> \\]');
    const blocks = Array.from(container.querySelectorAll('.markdown-math-display'));
    expect(blocks.map((block) => block.querySelector('.katex-display annotation')!.textContent!.trim())).toEqual(['y=1', '\\frac{1}{2}', 'z']);
    expect(container.querySelector('ol .markdown-math-display')).not.toBeNull();
    expect(container.querySelector('blockquote .markdown-math-display')).not.toBeNull();
  });

  it('独占行 \\[ \\] 内的 - 、1. 、> 开头行不被当成 Markdown 块语法', () => {
    const container = renderMarkdown('\\[\nx\n- y\n1. z\n> w\n\\]\n\n- 列表项\n\n  \\[\n  a\n  - b\n  \\]');
    const blocks = Array.from(container.querySelectorAll('.markdown-math-display'));
    expect(blocks.map((block) => block.querySelector('annotation')!.textContent!.replace(/\s+/g, ' ').trim()))
      .toEqual(['x - y 1. z > w', 'a - b']);
    expect(container.querySelectorAll('li')).toHaveLength(1);
    expect(container.querySelector('blockquote')).toBeNull();
  });

  it('预判只剥掉与 \\[ 行同层数的引用标记，独占一行的 > 属于公式原文', () => {
    const container = renderMarkdown('\\[\na\n>\nb\n\\]\n\n- c\n\n> \\[\n> x\n> >\n> y\n> \\]');
    expect(Array.from(container.querySelectorAll('.markdown-math-display annotation')).map((node) => node.textContent!.replace(/\s+/g, ' ').trim()))
      .toEqual(['a > b', 'x > y']);
    expect(container.querySelector('li')?.textContent).toBe('c');
    expect(container.querySelectorAll('blockquote')).toHaveLength(1);
    expect(container.querySelector('blockquote .markdown-math-display')).not.toBeNull();

    const quotedBlankLine = renderMarkdown('> \\[\n> x\n>\n> \\]');
    expect(quotedBlankLine.querySelector('.katex')).toBeNull();
  });

  it('独占行 \\[ 在空行前没有闭合符时不进入公式块，后文列表、引用保持结构', () => {
    const streaming = renderMarkdown('前文\n\\[\nx\n- y\n- z\n\n> 引用\n\n后文段落');
    expect(streaming.querySelector('.katex')).toBeNull();
    expect(Array.from(streaming.querySelectorAll('li')).map((li) => li.textContent)).toEqual(['y', 'z']);
    expect(streaming.querySelector('blockquote')?.textContent?.trim()).toBe('引用');

    const blankBeforeClose = renderMarkdown('\\[\nx\n\n- a\n\n\\]');
    expect(blankBeforeClose.querySelector('.markdown-math-display')).toBeNull();
    expect(blankBeforeClose.querySelector('li')?.textContent).toBe('a');
  });

  it.each([
    ['无序列表', '- \\[\n  x\n  - a\n- next\n  \\[\n  y\n  \\]'],
    ['有序列表', '1. \\[\n   x\n   - a\n2. next\n   \\[\n   y\n   \\]'],
    ['引用内列表', '> - \\[\n>   x\n>   - a\n> - next\n>   \\[\n>   y\n>   \\]'],
  ])('%s 中未闭合公式不借用下一列表项的闭合符，子列表仍保留', (_name, source) => {
    const container = renderMarkdown(source);
    expect(container.querySelector('li li')?.textContent).toBe('a');
    expect(container.querySelectorAll('li')).toHaveLength(3);
    expect(tex(container)).toEqual(['y']);
    expect(container.textContent).toContain('next');
  });

  it('行尾 \\] 闭合公式块，后文列表、引用、标题与下一条公式保持结构', () => {
    const container = renderMarkdown('\\[\nx \\]\n\n- a\n\n> b\n\n## h\n\n\\[\ny\n\\]\n\n\\[\na \\\\\nb \\\\]\n\\]');
    expect(Array.from(container.querySelectorAll('.markdown-math-display annotation')).map((node) => node.textContent!.trim()))
      .toEqual(['x', 'y', 'a \\\\\nb \\\\]']);
    expect(container.querySelector('li')?.textContent).toBe('a');
    expect(container.querySelector('blockquote')?.textContent?.trim()).toBe('b');
    expect(container.querySelector('h2')?.textContent).toBe('h');
    expect(container.querySelector('.katex-error')).toBeNull();
  });

  it('闭合符后的全角空格、NBSP 与 Markdown 一样算正文：不构成公式，后文结构不受影响', () => {
    for (const space of ['\u3000', '\u00a0']) {
      for (const [source, paragraph] of [
        [`\\[\nx \\]${space}\n\n- a\n- b\n\n> q`, `[\nx ]${space}`],
        [`\\[\nx\n\\]${space}\n\n- a\n- b\n\n> q`, `[\nx\n]${space}`],
      ]) {
        const container = renderMarkdown(source);
        expect(container.querySelector('.katex')).toBeNull();
        expect(container.querySelector('p')?.textContent).toBe(paragraph);
        expect(Array.from(container.querySelectorAll('li')).map((li) => li.textContent)).toEqual(['a', 'b']);
        expect(container.querySelector('blockquote')?.textContent?.trim()).toBe('q');
      }
    }
    const tabAfter = renderMarkdown('\\[\nx \\]\t\n\n- a');
    expect(tabAfter.querySelector('.markdown-math-display annotation')?.textContent?.trim()).toBe('x');
    expect(tabAfter.querySelector('li')?.textContent).toBe('a');
  });

  it('段内 \\( \\)、与文字同段但独占一行的 \\[ \\] 与跨行写法渲染为行内公式', () => {
    const container = renderMarkdown('行内 \\(x^2\\) 而是：\n\\[ a \\times b \\]\n这样，还有 \\(a\nb\\)。');
    expect(container.querySelector('.katex-display')).toBeNull();
    expect(tex(container)).toEqual(['x^2', 'a \\times b', 'a\nb']);
  });

  it('独占一行的 \\[ \\] 允许续行缩进与行尾空白，表格单元格中独占时同样识别', () => {
    const container = renderMarkdown('其中：\n   \\[ x \\]  \n后文\n\n| 公式 |\n|---|\n| \\[ y \\] |');
    expect(tex(container)).toEqual(['x', 'y']);
    expect(container.querySelector('.katex-display')).toBeNull();
  });

  it('与文字同一行的 \\[…\\] 是转义方括号，不识别为公式', () => {
    const container = renderMarkdown([
      '参考文献见\\[1\\]和\\[2\\]。',
      '\\[3\\] 张三，2020。',
      '前文 \\[ a\n\\]',
      '**\\[注\\]**',
      '| 引用 |\n|---|\n| 见\\[4\\] |',
    ].join('\n\n'));
    expect(container.querySelector('.katex')).toBeNull();
    expect(Array.from(container.querySelectorAll('p, td')).map((node) => node.textContent)).toEqual([
      '参考文献见[1]和[2]。', '[3] 张三，2020。', '前文 [ a\n]', '[注]', '见[4]',
    ]);
  });

  it('独占一段的 \\[…\\] 仍是独立公式', () => {
    const container = renderMarkdown('\\[3\\]\n\n- \\[ x^2 \\]');
    expect(Array.from(container.querySelectorAll('.markdown-math-display annotation')).map((node) => node.textContent!.trim()))
      .toEqual(['3', 'x^2']);
  });

  it('LaTeX 换行 \\\\ 与 \\\\[2pt] 不会提前闭合', () => {
    const container = renderMarkdown('\\(a \\\\ b\\) 与\n\\[ \\begin{aligned} a \\\\[2pt] b \\end{aligned} \\]');
    expect(tex(container)).toEqual(['a \\\\ b', '\\begin{aligned} a \\\\[2pt] b \\end{aligned}']);
  });

  it('链接与图片目标中的转义括号保持原样', () => {
    const container = renderMarkdown('[报告](报告\\(终版\\).pdf) ![图](图\\(1\\).png)');
    expect(decodeURI(container.querySelector('a')!.getAttribute('href')!)).toBe('报告(终版).pdf');
    expect(decodeURI(container.querySelector('img')!.getAttribute('src')!)).toBe('图(1).png');
    expect(container.querySelector('.katex')).toBeNull();
  });

  it('围栏、缩进、列表内围栏代码与行内代码保持原文，之后的公式照常渲染', () => {
    const container = renderMarkdown([
      '```\n\\(fenced\\)\n```',
      '    \\(indented\\)',
      '- ```\n  \\(listed\\)\n  ```',
      '用 `\\(inline\\)` 表示',
      '之后 \\(y\\)',
    ].join('\n\n'));
    const codeText = Array.from(container.querySelectorAll('code')).map((node) => node.textContent);
    expect(codeText).toEqual(['\\(fenced\\)\n', '\\(indented\\)\n', '\\(listed\\)\n', '\\(inline\\)']);
    expect(tex(container)).toEqual(['y']);
  });

  it('未闭合的分隔符回退为普通转义字符，跨空行不成对', () => {
    const container = renderMarkdown('流式中 \\[ x\n\n\\( a\n\nb \\)');
    expect(container.querySelector('.katex')).toBeNull();
    expect(container.textContent).toContain('流式中 [ x');
  });

  it('行内公式片段可在超宽时局部滚动，并按 strut 深度保持基线对齐', () => {
    const container = renderMarkdown('段内 \\(\\frac{\\text{很长的分子}}{\\text{很长的分母}} + x\\)\n\n$$\n\\frac{1}{2}\n$$');
    const inlineBases = Array.from(container.querySelectorAll('p .katex .base')) as HTMLElement[];
    expect(inlineBases.every((base) => base.classList.contains('markdown-math-inline-base'))).toBe(true);
    const strutAlign = (base: HTMLElement) => (base.firstElementChild as HTMLElement).style.verticalAlign || '0';
    expect(inlineBases.map((base) => base.style.verticalAlign)).toEqual(inlineBases.map(strutAlign));
    expect(inlineBases[0].style.verticalAlign).not.toBe('0');
    expect(container.querySelector('.katex-display .markdown-math-inline-base')).toBeNull();
  });

  it('只有 language-math 代码按公式渲染，其他语言名包含 math 的代码保持原文', () => {
    const container = renderMarkdown('```mathematica\nx^2\n```\n\n```math\ny^2\n```');
    expect(container.querySelector('code.language-mathematica')?.textContent).toBe('x^2\n');
    expect(tex(container)).toEqual(['y^2']);
    expect(container.querySelector('.markdown-math-display .katex-display')).not.toBeNull();
  });

  it.each([
    ['流式中刚打开的 $$ 块', '推导如下：\n\n$$'],
    ['空的 $$ 块', '$$\n$$'],
    ['空的 math 围栏', '```math\n```'],
    ['空的行内公式', '前 \\(\\) 后'],
  ])('%s 不显示任何文字', (_name, source) => {
    const container = renderMarkdown(source);
    expect(container.querySelector('.katex')).not.toBeNull();
    expect(Array.from(container.querySelectorAll('.katex-html')).map((node) => node.textContent)).toEqual(['']);
    expect(container.textContent).not.toContain('undefined');
  });

  it('KaTeX 内部错误只降级这一条公式，其余正文与公式照常渲染', () => {
    const cd = '\\begin{CD} A @>f>> B @ \\end{CD}';
    const container = renderMarkdown(`交换图：\n\n$$\n${cd}\n$$\n\n其他正文 \\(x^2\\)`);
    const error = container.querySelector('.markdown-math-display .katex-error') as HTMLElement;
    expect(error.textContent).toBe(cd);
    expect(error.getAttribute('title')).toMatch(/Error/);
    expect(error.style.color).toBe('rgb(107, 100, 89)');
    expect(tex(container)).toEqual(['x^2']);
  });

  it('公式 HTML 不输出链接与原始标签', () => {
    const container = renderMarkdown('\\(\\href{javascript:alert(1)}{x}\\) 与 $$<img src=x onerror=alert(1)>$$');
    expect(container.querySelector('a, img')).toBeNull();
    expect(container.querySelectorAll('.katex')).toHaveLength(2);
  });

  it('流式追加正文时，未变化的公式不重新渲染', () => {
    const renderToString = vi.spyOn(katex, 'renderToString');
    const first = '前文 \\(x^2\\)\n\n$$\n\\frac{1}{2}\n$$\n\n后续';
    const { container, rerender } = render(markdown(first));
    const [inline, display] = Array.from(container.querySelectorAll('.katex'));
    expect(renderToString).toHaveBeenCalledTimes(2);

    rerender(markdown(`${first}正文继续`));
    expect(renderToString).toHaveBeenCalledTimes(2);
    const [inlineAfter, displayAfter] = Array.from(container.querySelectorAll('.katex'));
    expect(inlineAfter).toBe(inline);
    expect(displayAfter).toBe(display);

    rerender(markdown(`${first}正文继续 \\(y\\)`));
    expect(renderToString).toHaveBeenCalledTimes(3);
    expect(renderToString).toHaveBeenLastCalledWith('y', expect.objectContaining({ displayMode: false }));
    renderToString.mockRestore();
  });
});
