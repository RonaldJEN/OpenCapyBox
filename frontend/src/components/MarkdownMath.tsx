import 'katex/dist/katex.min.css';
import katex from 'katex';
import { memo } from 'react';
import type { Components, ExtraProps } from 'react-markdown';

type HastElement = NonNullable<ExtraProps['node']>;
type HastText = Extract<HastElement['children'][number], { type: 'text' }>;

const KATEX_ERROR_COLOR = '#6B6459';

/**
 * 行内公式里分式、矩阵等不可拆片段（KaTeX `.base`）超宽时在片段内横向滚动。
 * 可滚动的 inline-block 基线落在底边，用 strut 记录的深度把底边放回基线下方，保持与正文对齐。
 */
function scrollableInlineMath(html: string) {
  const template = document.createElement('template');
  template.innerHTML = html;
  template.content.querySelectorAll<HTMLElement>('.base').forEach((base) => {
    // 首个子节点是 strut；深度为 0 时 KaTeX 不输出 vertical-align。
    const strut = base.firstElementChild as HTMLElement;
    base.classList.add('markdown-math-inline-base');
    base.style.verticalAlign = strut.style.verticalAlign || '0';
  });
  return template.innerHTML;
}

/**
 * 公式按 LaTeX 原文与显示模式渲染为 KaTeX HTML。memo 让流式追加正文时未变化的公式整段跳过，
 * 不再逐个 span 重建与比对。KaTeX 默认 `trust: false`，`\href`、`\htmlClass` 等不会输出可执行内容，HTML 可直接注入。
 * 解析失败只降级这一条公式为 `.katex-error` 原文（次要文字色，title 为错误信息），整条消息或整份预览照常渲染。
 */
export const MarkdownMath = memo(function MarkdownMath({ tex, display }: { tex: string; display: boolean }) {
  let html: string;
  try {
    html = katex.renderToString(tex, { displayMode: display, throwOnError: false, strict: 'ignore', errorColor: KATEX_ERROR_COLOR });
  } catch (error) {
    // throwOnError: false 只处理 ParseError；KaTeX 内部断言错误（如 CD 图行尾多出的 `@`）仍会抛出，
    // 按 spec 与 ParseError 同样在节点内显示错误，原 rehype-katex 也是这样处理。
    return <span className="katex-error" style={{ color: KATEX_ERROR_COLOR }} title={String(error)}>{tex}</span>;
  }
  return display
    ? <div dangerouslySetInnerHTML={{ __html: html }} />
    : <span dangerouslySetInnerHTML={{ __html: scrollableInlineMath(html) }} />;
});

/**
 * `code.language-math` 渲染为公式，其余代码返回 null 交给调用方。
 * 公式原文取自语法树：React children 会丢掉空字符串，空公式（含流式中刚打开的 `$$`）会变成 undefined。
 */
export function markdownMathElement(className: string | undefined, node: HastElement) {
  const classes = className?.split(' ') ?? [];
  return classes.includes('language-math')
    ? <MarkdownMath tex={(node.children as HastText[]).map((child) => child.value).join('')} display={classes.includes('math-display')} />
    : null;
}

// 没有自定义 code 渲染的 Markdown 视图直接使用。
export const markdownMathComponents: Components = {
  code: ({ node, className, children, ...props }) => (
    markdownMathElement(className, node!) ?? <code className={className} {...props}>{children}</code>
  ),
};
