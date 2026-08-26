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
    ].join('\n'))).toEqual([
      { depth: 1, text: '核心结论', id: '核心结论' },
      { depth: 2, text: '持仓结构', id: '持仓结构' },
      { depth: 2, text: '持仓结构', id: '持仓结构-2' },
    ]);
  });
});
