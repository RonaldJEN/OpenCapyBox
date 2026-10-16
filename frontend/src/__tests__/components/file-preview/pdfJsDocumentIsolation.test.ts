import { describe, expect, it } from 'vitest';
// 测试进程是 Node.js，按 pdf.js 要求使用 legacy build；它与浏览器 build 来自同一份源码。
import { getDocument } from 'pdfjs-dist/legacy/build/pdf.mjs';

/** 生成只含空白页的最小合法 PDF（xref 偏移按字节精确计算）。 */
function blankPdf(pageCount: number): Uint8Array {
  const pageRefs = Array.from({ length: pageCount }, (_, index) => `${index + 3} 0 R`).join(' ');
  const objects = [
    '<< /Type /Catalog /Pages 2 0 R >>',
    `<< /Type /Pages /Kids [${pageRefs}] /Count ${pageCount} >>`,
    ...Array.from({ length: pageCount }, () => '<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] >>'),
  ];
  let body = '%PDF-1.4\n';
  const offsets = objects.map((object, index) => {
    const offset = body.length;
    body += `${index + 1} 0 obj\n${object}\nendobj\n`;
    return offset;
  });
  const xrefOffset = body.length;
  body += `xref\n0 ${objects.length + 1}\n0000000000 65535 f \n`;
  body += offsets.map((offset) => `${String(offset).padStart(10, '0')} 00000 n \n`).join('');
  body += `trailer\n<< /Size ${objects.length + 1} /Root 1 0 R >>\nstartxref\n${xrefOffset}\n%%EOF\n`;
  return new TextEncoder().encode(body);
}

describe('pdfjs-dist 多文档隔离', () => {
  // pdfjs-dist 5.4 的 PagesMapper 是全局单例：后打开的文档会改写所有文档共用的页数，
  // 先打开文档中超出新页数的页面随即 getPage 失败（PPTX 打开 DOCX 后幻灯片“无法预览”）。
  it('后打开或销毁的文档不影响已打开文档的页码边界', async () => {
    const deckTask = getDocument({ data: blankPdf(5) });
    const deck = await deckTask.promise;
    const reportTask = getDocument({ data: blankPdf(2) });
    const report = await reportTask.promise;

    await expect(deck.getPage(5)).resolves.toMatchObject({ pageNumber: 5 });
    await expect(report.getPage(2)).resolves.toMatchObject({ pageNumber: 2 });

    await reportTask.destroy();
    await expect(deck.getPage(4)).resolves.toMatchObject({ pageNumber: 4 });
    await deckTask.destroy();
  });
});
