import { describe, expect, it } from 'vitest';
import { vditorOutlineHeadings } from '../../../components/file-preview/markdownOutline';

const ZWSP = String.fromCharCode(0x200b);

// 取自 Vditor 3.11 / Lute 的 Md2VditorDOM 输出（预览区在真实画布中会再填入 KaTeX 渲染结果）。
const VDITOR_DOM = [
  '<div class="vditor-wysiwyg__block" data-type="yaml-front-matter" data-block="0"><pre><code data-type="yaml-front-matter">title: 季度报告</code></pre></div>',
  '<h1 data-block="0" data-marker="#"><img src="logo.png" alt="OpenCapyBox" /></h1>',
  `<h2 data-block="0" data-marker="#">风险 <span class="vditor-wysiwyg__block" data-type="math-inline"><code data-type="math-inline" style="display: none">${ZWSP}\\sigma</code><span class="vditor-wysiwyg__preview" data-render="1"><span class="katex">σ</span></span></span>${ZWSP} 与 <span data-type="backslash"><span>\\</span>(</span>x<span data-type="backslash"><span>\\</span>)</span></h2>`,
  `<h2 data-block="0" data-marker="#">${ZWSP}<code data-marker="\`">${ZWSP}code</code>${ZWSP} 标题</h2>`,
  '<div class="vditor-toc" data-block="0" data-type="toc-block" contenteditable="false"><ul><li><span>OpenCapyBox</span></li></ul></div>',
  '<div class="vditor-wysiwyg__block" data-type="html-block" data-block="0"><pre><code>&lt;h2&gt;HTML 标题&lt;/h2&gt;</code></pre><pre class="vditor-wysiwyg__preview" data-render="2"><h2>HTML 标题</h2></pre></div>',
  '<h2 data-block="0" data-marker="#"></h2>',
  '<blockquote data-block="0"><h3 data-block="0" data-marker="#">引用标题</h3></blockquote>',
  '<h2 data-block="0" data-marker="-">结论</h2>',
  '<div data-block="0" data-type="footnotes-block"><ol data-type="footnotes-defs-ol"><li data-type="footnotes-li" data-marker="^a"><h2 data-block="0" data-marker="#">脚注标题</h2></li></ol></div>',
  '<h4 data-block="0" data-marker="#">四级</h4>',
].join('');

describe('vditorOutlineHeadings', () => {
  it('按画布中的正文标题生成目录：跳过 front matter、目录块、HTML 预览、脚注、空标题与 4 级以下标题', () => {
    const root = document.createElement('pre');
    root.innerHTML = VDITOR_DOM;
    const headings = vditorOutlineHeadings(root);
    expect(headings.map(({ depth, text }) => ({ depth, text }))).toEqual([
      { depth: 1, text: 'OpenCapyBox' },
      { depth: 2, text: '风险 \\sigma 与 (x)' },
      { depth: 2, text: 'code 标题' },
      { depth: 3, text: '引用标题' },
      { depth: 2, text: '结论' },
    ]);
    const topLevelH2 = Array.from(root.querySelectorAll(':scope > h2'));
    const targets = [root.querySelector('h1'), topLevelH2[0], topLevelH2[1], root.querySelector('blockquote h3'), topLevelH2[topLevelH2.length - 1]];
    headings.forEach(({ element }, index) => expect(element).toBe(targets[index]));
  });
});
