import { describe, expect, it } from 'vitest';
import { extractAssistantContentBlocks } from '../../utils/assistantFileRefs';

describe('assistantFileRefs', () => {
  it('将当前 session 的绝对文件位置解析为文件卡片 block', () => {
    const blocks = extractAssistantContentBlocks(
      [
        '写好了，领导！',
        '',
        '**文件位置：** `/home/user/sessions/session-1/quick_sort.py`',
        '',
        '包含两个版本：',
      ].join('\n'),
      'session-1',
    );

    expect(blocks).toHaveLength(3);
    expect(blocks[0]).toMatchObject({ type: 'markdown', content: '写好了，领导！' });
    expect(blocks[1]).toMatchObject({
      type: 'file',
      file: {
        name: 'quick_sort.py',
        path: 'quick_sort.py',
        type: 'py',
        session_id: 'session-1',
      },
    });
    expect(blocks[2]).toMatchObject({ type: 'markdown', content: '\n包含两个版本：' });
  });

  it('支持相对路径并按 path 去重', () => {
    const blocks = extractAssistantContentBlocks(
      ['文件路径： results/report.md', '文件路径： `results/report.md`'].join('\n'),
      'session-1',
    );

    expect(blocks).toHaveLength(1);
    expect(blocks[0]).toMatchObject({
      type: 'file',
      file: { name: 'report.md', path: 'results/report.md' },
    });
  });

  it('支持句子里的反引号文件引用', () => {
    const blocks = extractAssistantContentBlocks('搞定！ `DeepSeek_V4_解读.docx` 已生成，22KB。', 'session-1');

    expect(blocks).toHaveLength(3);
    expect(blocks[0]).toMatchObject({ type: 'markdown', content: '搞定！' });
    expect(blocks[1]).toMatchObject({
      type: 'file',
      file: {
        name: 'DeepSeek_V4_解读.docx',
        path: 'DeepSeek_V4_解读.docx',
        type: 'docx',
        session_id: 'session-1',
      },
    });
    expect(blocks[2]).toMatchObject({ type: 'markdown', content: ' 已生成，22KB。' });
  });

  it('支持 FilePreview 当前处理的常见文件类型', () => {
    const blocks = extractAssistantContentBlocks(
      [
        '文件： `report.pdf`',
        '文件： `notes.md`',
        '文件： `data.csv`',
        '文件： `page.html`',
        '文件： `image.png`',
        '文件： `config.json`',
      ].join('\n'),
      'session-1',
    );

    const files = blocks.filter((block) => block.type === 'file').map((block) => block.file.name);
    expect(files).toEqual(['report.pdf', 'notes.md', 'data.csv', 'page.html', 'image.png', 'config.json']);
  });

  it('不会把不支持预览的反引号内容转成文件卡片', () => {
    const content = '压缩包 `archive.zip` 已生成。';
    const blocks = extractAssistantContentBlocks(content, 'session-1');

    expect(blocks).toEqual([{ type: 'markdown', content }]);
  });

  it('不会解析 fenced code block 内的文件提示', () => {
    const content = ['```text', '文件位置： quick_sort.py', '```'].join('\n');
    const blocks = extractAssistantContentBlocks(content, 'session-1');

    expect(blocks).toEqual([{ type: 'markdown', content }]);
  });

  it('拒绝跨 session 的绝对路径并保留原 markdown', () => {
    const content = '文件位置： /home/user/sessions/other-session/quick_sort.py';
    const blocks = extractAssistantContentBlocks(content, 'session-1');

    expect(blocks).toEqual([{ type: 'markdown', content }]);
  });

  it('没有 sessionId 时不生成文件卡片', () => {
    const content = '文件位置： quick_sort.py';
    const blocks = extractAssistantContentBlocks(content);

    expect(blocks).toEqual([{ type: 'markdown', content }]);
  });
});