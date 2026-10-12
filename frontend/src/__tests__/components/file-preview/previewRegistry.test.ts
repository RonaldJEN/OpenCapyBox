import { describe, expect, it } from 'vitest';
import { extractMarkdownHeadings } from '../../../components/file-preview/MarkdownReportPreview';
import {
  resolvePreviewDescriptor,
  withRenderFormat,
} from '../../../components/file-preview/previewRegistry';

describe('file preview registry', () => {
  it.each([
    ['report.md', 'text/markdown', 'markdown'],
    ['page.html', 'text/html', 'html'],
    ['report.docx', 'application/vnd.openxmlformats-officedocument.wordprocessingml.document', 'document'],
    ['deck.pptx', 'application/vnd.openxmlformats-officedocument.presentationml.presentation', 'presentation'],
    ['deck.slides', 'application/octet-stream', 'presentation'],
    ['metrics.xlsx', 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', 'spreadsheet'],
    ['budget.et', 'application/octet-stream', 'spreadsheet'],
    ['settings.cfg', '', 'code'],
  ])('routes %s to the %s adapter', (name, type, expectedKind) => {
    expect(resolvePreviewDescriptor({ name, type }).kind).toBe(expectedKind);
  });

  it('preserves existing query/hash and replaces an existing renderer', () => {
    expect(withRenderFormat('/file?preview=true#page=2', 'pdf')).toBe('/file?preview=true&render=pdf#page=2');
    expect(withRenderFormat('/file?preview=true&render=html#page=2', 'pdf')).toBe('/file?preview=true&render=pdf#page=2');
  });
});

describe('Markdown report headings', () => {
  it('builds stable Chinese anchors, deduplicates titles and ignores fenced code', () => {
    expect(extractMarkdownHeadings([
      '# 核心结论',
      '## 持仓结构',
      '```md',
      '# 不是标题',
      '```',
      '## 持仓结构',
    ].join('\n'))).toMatchObject([
      { depth: 1, text: '核心结论', id: '核心结论' },
      { depth: 2, text: '持仓结构', id: '持仓结构' },
      { depth: 2, text: '持仓结构', id: '持仓结构-2' },
    ]);
  });

  it('按语法树识别列表内代码块之后的标题、嵌套标题与 setext 标题，并记录源偏移', () => {
    const content = [
      '# 开始',
      '',
      '- 列表',
      '',
      '  ```',
      '  ## 代码里不是标题',
      '  ```',
      '',
      '## 结束 **加粗** [链接](a.md)',
      '',
      '> ### 引用标题',
      '',
      '概览',
      '---',
    ].join('\n');
    const headings = extractMarkdownHeadings(content);
    expect(headings.map(({ depth, text, id }) => ({ depth, text, id }))).toEqual([
      { depth: 1, text: '开始', id: '开始' },
      { depth: 2, text: '结束 加粗 链接', id: '结束-加粗-链接' },
      { depth: 3, text: '引用标题', id: '引用标题' },
      { depth: 2, text: '概览', id: '概览' },
    ]);
    expect(headings[1].offset).toBe(content.indexOf('## 结束'));
  });

  it('同一份原文连续提取只解析一次，原文变化后重新解析', () => {
    const first = extractMarkdownHeadings('# 甲\n\n## 乙');
    expect(extractMarkdownHeadings('# 甲\n\n## 乙')).toBe(first);
    const changed = extractMarkdownHeadings('# 甲\n\n## 丙');
    expect(changed).not.toBe(first);
    expect(changed.map((heading) => heading.text)).toEqual(['甲', '丙']);
  });
});
