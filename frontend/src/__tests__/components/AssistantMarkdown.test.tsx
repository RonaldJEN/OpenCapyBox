import { describe, expect, it, vi } from 'vitest';
import { render } from '@testing-library/react';
import { AssistantMarkdown } from '../../components/AssistantMarkdown';

function renderMarkdown(content: string) {
  return render(<AssistantMarkdown content={content} readingNodeId="n1" fileReferences={[]} />).container;
}

describe('AssistantMarkdown 公式渲染', () => {
  it('段内 $$…$$ 与中文 \\text 渲染为公式，前后文字和列表编号保留', () => {
    const warn = vi.spyOn(console, 'warn');
    const container = renderMarkdown('1. **送达耗时** 而是：\n$$ \\text{发票价格} = \\text{期货结算价} \\times \\text{转换因子} $$\n这样无论交割哪只券。');
    const item = container.querySelector('ol > li')!;
    expect(item.querySelector('.katex annotation')?.textContent).toBe('\\text{发票价格} = \\text{期货结算价} \\times \\text{转换因子}');
    expect(item.textContent).toContain('而是：');
    expect(item.textContent).toContain('这样无论交割哪只券。');
    expect(warn).not.toHaveBeenCalled();
    warn.mockRestore();
  });

  it('\\( \\) 渲染为行内公式，\\[ \\] 独占行渲染为带阅读锚点的独立公式', () => {
    const source = '前文 \\(x^2\\)\n\n\\[\n\\frac{1}{2}\n\\]';
    const container = renderMarkdown(source);
    expect(container.querySelector('p .katex annotation')?.textContent).toBe('x^2');
    const block = container.querySelector('.markdown-math-display')!;
    expect(block.querySelector('.katex-display annotation')!.textContent!.trim()).toBe('\\frac{1}{2}');
    expect(block.getAttribute('data-reading-block')).toBe(`answer:n1:div:${source.indexOf('\\[')}`);
  });

  it('单个 $ 金额、行内代码与代码块保持原文', () => {
    const container = renderMarkdown('价格 $100 到 $200 之间，代码 `$$x$$`\n\n```latex\n$$ y $$\n```');
    expect(container.querySelector('.katex')).toBeNull();
    expect(container.textContent).toContain('价格 $100 到 $200 之间');
    expect(container.textContent).toContain('$$x$$');
    expect(container.textContent).toContain('$$ y $$');
  });

  it('未闭合公式局部显示原文，不抛错影响整段', () => {
    const container = renderMarkdown('前文\n\n$$\n\\frac{1}{');
    expect(container.textContent).toContain('前文');
    expect(container.querySelector('.katex-error')?.textContent).toContain('\\frac{1}{');
  });

  it('全角标点紧邻的加粗按 CJK 规则生效', () => {
    const container = renderMarkdown('卖方会选择**对自己最有利（成本最低）**的那只券');
    expect(container.querySelector('strong')?.textContent).toBe('对自己最有利（成本最低）');
  });

  it('流式中刚打开的 $$ 不显示 undefined，KaTeX 内部错误只降级该公式', () => {
    expect(renderMarkdown('推导如下：\n\n$$').textContent).not.toContain('undefined');
    const container = renderMarkdown('交换图：\n\n$$\n\\begin{CD} A @>f>> B @ \\end{CD}\n$$\n\n其他正文 \\(x^2\\)');
    expect(container.querySelector('.markdown-math-display .katex-error')).not.toBeNull();
    expect(container.querySelector('p .katex annotation')?.textContent).toBe('x^2');
  });

  it('流式追加时已渲染的公式节点保持不变，math 围栏渲染为独立公式而非代码块', () => {
    const view = (content: string) => <AssistantMarkdown content={content} readingNodeId="n1" fileReferences={[]} />;
    const first = '行内 \\(x^2\\)\n\n```math\n\\frac{1}{2}\n```\n\n后续';
    const { container, rerender } = render(view(first));
    const [inline, display] = Array.from(container.querySelectorAll('.katex'));
    expect(display.closest('.markdown-math-display')?.getAttribute('data-reading-block')).toBe(`answer:n1:div:${first.indexOf('```math')}`);
    expect(container.querySelector('pre, [data-reading-block] code')).toBeNull();

    rerender(view(`${first}正文继续`));
    const [inlineAfter, displayAfter] = Array.from(container.querySelectorAll('.katex'));
    expect(inlineAfter).toBe(inline);
    expect(displayAfter).toBe(display);
  });
});
