import { describe, expect, it } from 'vitest';
import { extractAssistantFiles } from '../../utils/assistantFileRefs';

describe('assistantFileRefs', () => {
  it('将当前 session 的绝对文件位置解析为文件', () => {
    const files = extractAssistantFiles(
      [
        '写好了，领导！',
        '',
        '**文件位置：** `/home/user/sessions/session-1/quick_sort.py`',
        '',
        '包含两个版本：',
      ].join('\n'),
      'session-1',
    );

    expect(files).toHaveLength(1);
    expect(files[0]).toMatchObject({
      name: 'quick_sort.py',
      path: 'quick_sort.py',
      type: 'py',
      session_id: 'session-1',
    });
  });

  it('支持相对路径并按 path 去重', () => {
    const files = extractAssistantFiles(
      ['文件路径： results/report.md', '文件路径： `results/report.md`'].join('\n'),
      'session-1',
    );

    expect(files).toHaveLength(1);
    expect(files[0]).toMatchObject({ name: 'report.md', path: 'results/report.md' });
  });

  it('支持句子里的反引号文件引用', () => {
    const files = extractAssistantFiles('搞定！ `DeepSeek_V4_解读.docx` 已生成，22KB。', 'session-1');

    expect(files).toHaveLength(1);
    expect(files[0]).toMatchObject({
      name: 'DeepSeek_V4_解读.docx',
      path: 'DeepSeek_V4_解读.docx',
      type: 'docx',
      session_id: 'session-1',
    });
  });

  it('支持被成对强调标记包裹的行内文件引用', () => {
    const files = extractAssistantFiles(
      '📄 **`/home/user/sessions/session-1/vlog-complete-production-guide.md`** — 366行，约15KB',
      'session-1',
    );

    expect(files).toHaveLength(1);
    expect(files[0]).toMatchObject({
      name: 'vlog-complete-production-guide.md',
      path: 'vlog-complete-production-guide.md',
    });
  });

  it('对列表内嵌文件引用按出现顺序去重', () => {
    const files = extractAssistantFiles(
      [
        '- `博通公司财务调研报告.docx`（初始版本）',
        '- `博通公司财务调研报告_最终版.docx`（推荐使用这个）',
        '- `博通公司财务调研报告.docx`（重复提及）',
      ].join('\n'),
      'session-1',
    );

    expect(files.map((file) => file.name)).toEqual([
      '博通公司财务调研报告.docx',
      '博通公司财务调研报告_最终版.docx',
    ]);
  });

  it('支持 FilePreview 当前处理的常见文件类型', () => {
    const files = extractAssistantFiles(
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

    expect(files.map((file) => file.name)).toEqual([
      'report.pdf',
      'notes.md',
      'data.csv',
      'page.html',
      'image.png',
      'config.json',
    ]);
  });

  it('不会把不支持预览的反引号内容当成文件', () => {
    const files = extractAssistantFiles('压缩包 `archive.zip` 已生成。', 'session-1');

    expect(files).toEqual([]);
  });

  it('不会解析 fenced code block 内的文件提示', () => {
    const files = extractAssistantFiles(['```text', '文件位置： quick_sort.py', '```'].join('\n'), 'session-1');

    expect(files).toEqual([]);
  });

  it('不会把行内命令中的文件名当成文件', () => {
    const files = extractAssistantFiles('运行 `python3 quick_sort.py` 看效果。', 'session-1');

    expect(files).toEqual([]);
  });

  it('拒绝跨 session 的绝对路径', () => {
    const files = extractAssistantFiles('文件位置： /home/user/sessions/other-session/quick_sort.py', 'session-1');

    expect(files).toEqual([]);
  });

  it('没有 sessionId 时不返回文件', () => {
    const files = extractAssistantFiles('文件位置： quick_sort.py');

    expect(files).toEqual([]);
  });
});