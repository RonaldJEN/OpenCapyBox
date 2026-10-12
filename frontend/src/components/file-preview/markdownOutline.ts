export interface MarkdownOutlineItem {
  depth: number;
  text: string;
}

// 不属于正文结构的标题：Vditor 脚注块、HTML 块预览。
const NON_DOCUMENT_HEADING_CONTAINERS = '[data-type="footnotes-block"], .vditor-wysiwyg__preview';

// 与 Vditor 画面一致的标题文字：公式取源码（去掉渲染预览），转义反斜杠与零宽占位不显示；纯图片标题取 alt。
function headingText(heading: HTMLElement) {
  const visible = heading.cloneNode(true) as HTMLElement;
  visible.querySelectorAll('.vditor-wysiwyg__preview, [data-type="backslash"] > span:first-child').forEach((node) => node.remove());
  const text = visible.textContent!.replace(/\u200b/g, '').replace(/\s+/g, ' ').trim();
  return text || Array.from(heading.querySelectorAll('img'), (image) => image.alt).join(' ').trim();
}

/**
 * Vditor 所见即所得 DOM 中的 1–3 级正文标题。目录条目与滚动目标都取自这里，
 * 不与 remark 的语法树按位置对齐：两套解析器对 YAML front matter、setext 等结构的判定不同。
 */
export function vditorOutlineHeadings(root: ParentNode) {
  return Array.from(root.querySelectorAll<HTMLElement>('h1, h2, h3'))
    .filter((heading) => !heading.closest(NON_DOCUMENT_HEADING_CONTAINERS))
    .map((element) => ({ element, depth: Number(element.tagName[1]), text: headingText(element) }))
    .filter((heading) => heading.text);
}
