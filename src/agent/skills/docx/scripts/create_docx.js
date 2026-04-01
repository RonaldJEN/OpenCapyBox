#!/usr/bin/env node
/**
 * create_docx.js - 创建格式精美的 Word 文档
 * 
 * 使用方法:
 *   node create_docx.js --from-json content.json -o output.docx
 *   node create_docx.js --from-md document.md -o output.docx
 *   node create_docx.js --from-md document.md --title "自定义标题" -o output.docx
 * 
 * JSON 格式示例:
 * {
 *   "title": "文档标题",
 *   "sections": [
 *     { "type": "heading1", "text": "第一章" },
 *     { "type": "paragraph", "text": "正文内容" },
 *     { "type": "bullet", "items": ["项目1", "项目2"] },
 *     { "type": "numbered", "items": ["步骤1", "步骤2"] },
 *     { "type": "heading2", "text": "1.1 小节" },
 *     { "type": "table", "headers": ["列1", "列2"], "rows": [["A", "B"], ["C", "D"]] }
 *   ]
 * }
 */

const { Document, Packer, Paragraph, TextRun, HeadingLevel, AlignmentType,
        LevelFormat, Table, TableRow, TableCell, BorderStyle, WidthType, 
        ShadingType, VerticalAlign, PageBreak } = require('docx');
const fs = require('fs');
const path = require('path');

// ============ 样式配置 ============
const STYLES = {
  default: { 
    document: { 
      run: { font: "Arial", size: 24 } // 12pt
    } 
  },
  paragraphStyles: [
    { 
      id: "Title", name: "Title", basedOn: "Normal",
      run: { size: 56, bold: true, font: "Arial" }, // 28pt
      paragraph: { spacing: { after: 300 }, alignment: AlignmentType.CENTER }
    },
    { 
      id: "Heading1", name: "Heading 1", basedOn: "Normal", next: "Normal", quickFormat: true,
      run: { size: 36, bold: true, font: "Arial", color: "1F4E79" }, // 18pt
      paragraph: { spacing: { before: 360, after: 180 }, outlineLevel: 0 }
    },
    { 
      id: "Heading2", name: "Heading 2", basedOn: "Normal", next: "Normal", quickFormat: true,
      run: { size: 28, bold: true, font: "Arial", color: "2E75B6" }, // 14pt
      paragraph: { spacing: { before: 240, after: 120 }, outlineLevel: 1 }
    },
    { 
      id: "Heading3", name: "Heading 3", basedOn: "Normal", next: "Normal", quickFormat: true,
      run: { size: 24, bold: true, font: "Arial", color: "404040" }, // 12pt
      paragraph: { spacing: { before: 180, after: 80 }, outlineLevel: 2 }
    },
  ]
};

// 列表编号配置
const NUMBERING = {
  config: [
    {
      reference: "bullet-list",
      levels: [{ 
        level: 0, format: LevelFormat.BULLET, text: "•", alignment: AlignmentType.LEFT,
        style: { paragraph: { indent: { left: 720, hanging: 360 } } }
      }]
    },
    {
      reference: "numbered-list",
      levels: [{ 
        level: 0, format: LevelFormat.DECIMAL, text: "%1.", alignment: AlignmentType.LEFT,
        style: { paragraph: { indent: { left: 720, hanging: 360 } } }
      }]
    }
  ]
};

// ============ 内容构建器 ============

function createTitle(text) {
  return new Paragraph({
    heading: HeadingLevel.TITLE,
    children: [new TextRun(text)]
  });
}

function createHeading(text, level) {
  const headingMap = {
    1: HeadingLevel.HEADING_1,
    2: HeadingLevel.HEADING_2,
    3: HeadingLevel.HEADING_3,
  };
  return new Paragraph({
    heading: headingMap[level] || HeadingLevel.HEADING_1,
    children: [new TextRun(text)]
  });
}

function createParagraph(text) {
  // 处理加粗和斜体
  const runs = parseInlineFormatting(text);
  return new Paragraph({
    spacing: { after: 120 },
    children: runs
  });
}

function parseInlineFormatting(text) {
  const runs = [];
  // 简单的加粗和斜体解析: **bold** *italic*
  const regex = /(\*\*(.+?)\*\*|\*(.+?)\*|([^*]+))/g;
  let match;
  
  while ((match = regex.exec(text)) !== null) {
    if (match[2]) {
      // 加粗 **text**
      runs.push(new TextRun({ text: match[2], bold: true }));
    } else if (match[3]) {
      // 斜体 *text*
      runs.push(new TextRun({ text: match[3], italics: true }));
    } else if (match[4]) {
      // 普通文本
      runs.push(new TextRun(match[4]));
    }
  }
  
  return runs.length > 0 ? runs : [new TextRun(text)];
}

function createBulletList(items) {
  return items.map(item => new Paragraph({
    numbering: { reference: "bullet-list", level: 0 },
    children: parseInlineFormatting(item)
  }));
}

function createNumberedList(items, listRef = "numbered-list") {
  return items.map(item => new Paragraph({
    numbering: { reference: listRef, level: 0 },
    children: parseInlineFormatting(item)
  }));
}

function createTable(headers, rows) {
  const columnCount = headers.length;
  const columnWidth = Math.floor(9360 / columnCount); // Letter 纸张可用宽度
  const columnWidths = Array(columnCount).fill(columnWidth);
  
  const tableBorder = { style: BorderStyle.SINGLE, size: 1, color: "CCCCCC" };
  const cellBorders = { top: tableBorder, bottom: tableBorder, left: tableBorder, right: tableBorder };
  
  // 表头行
  const headerRow = new TableRow({
    tableHeader: true,
    children: headers.map(header => new TableCell({
      borders: cellBorders,
      width: { size: columnWidth, type: WidthType.DXA },
      shading: { fill: "D5E8F0", type: ShadingType.CLEAR },
      verticalAlign: VerticalAlign.CENTER,
      children: [new Paragraph({
        alignment: AlignmentType.CENTER,
        children: [new TextRun({ text: header, bold: true })]
      })]
    }))
  });
  
  // 数据行
  const dataRows = rows.map(row => new TableRow({
    children: row.map(cell => new TableCell({
      borders: cellBorders,
      width: { size: columnWidth, type: WidthType.DXA },
      children: [new Paragraph({ children: [new TextRun(String(cell))] })]
    }))
  }));
  
  return new Table({
    columnWidths: columnWidths,
    rows: [headerRow, ...dataRows]
  });
}

// ============ JSON 解析器 ============

function parseJsonContent(json) {
  const children = [];
  
  // 添加标题
  if (json.title) {
    children.push(createTitle(json.title));
  }
  
  // 添加内容
  if (json.sections) {
    let numberedListCounter = 0;
    
    for (const section of json.sections) {
      switch (section.type) {
        case 'heading1':
          children.push(createHeading(section.text, 1));
          break;
        case 'heading2':
          children.push(createHeading(section.text, 2));
          break;
        case 'heading3':
          children.push(createHeading(section.text, 3));
          break;
        case 'paragraph':
          children.push(createParagraph(section.text));
          break;
        case 'bullet':
          children.push(...createBulletList(section.items));
          break;
        case 'numbered':
          // 每个 numbered 使用不同的 reference 以重置编号
          numberedListCounter++;
          const listRef = `numbered-list-${numberedListCounter}`;
          NUMBERING.config.push({
            reference: listRef,
            levels: [{ 
              level: 0, format: LevelFormat.DECIMAL, text: "%1.", alignment: AlignmentType.LEFT,
              style: { paragraph: { indent: { left: 720, hanging: 360 } } }
            }]
          });
          children.push(...createNumberedList(section.items, listRef));
          break;
        case 'table':
          children.push(createTable(section.headers, section.rows));
          break;
        case 'pagebreak':
          children.push(new Paragraph({ children: [new PageBreak()] }));
          break;
        default:
          console.warn(`Unknown section type: ${section.type}`);
      }
    }
  }
  
  return children;
}

// ============ Markdown 解析器 ============

function parseMarkdown(mdContent, customTitle = null) {
  const lines = mdContent.split('\n');
  const children = [];
  let i = 0;
  let numberedListCounter = 0;
  let hasTitle = false;
  
  while (i < lines.length) {
    const line = lines[i].trimEnd();
    
    // 空行
    if (line.trim() === '') {
      i++;
      continue;
    }
    
    // 标题
    const headingMatch = line.match(/^(#{1,6})\s+(.+)$/);
    if (headingMatch) {
      const level = headingMatch[1].length;
      const text = headingMatch[2];
      
      // 第一个 # 作为文档标题（或使用自定义标题）
      if (level === 1 && !hasTitle) {
        children.push(createTitle(customTitle || text));
        hasTitle = true;
      } else {
        children.push(createHeading(text, Math.min(level, 3)));
      }
      i++;
      continue;
    }
    
    // 无序列表
    if (line.match(/^[-*+]\s+/)) {
      const items = [];
      while (i < lines.length && lines[i].match(/^[-*+]\s+/)) {
        items.push(lines[i].replace(/^[-*+]\s+/, '').trim());
        i++;
      }
      children.push(...createBulletList(items));
      continue;
    }
    
    // 有序列表
    if (line.match(/^\d+\.\s+/)) {
      const items = [];
      while (i < lines.length && lines[i].match(/^\d+\.\s+/)) {
        items.push(lines[i].replace(/^\d+\.\s+/, '').trim());
        i++;
      }
      numberedListCounter++;
      const listRef = `numbered-list-${numberedListCounter}`;
      NUMBERING.config.push({
        reference: listRef,
        levels: [{ 
          level: 0, format: LevelFormat.DECIMAL, text: "%1.", alignment: AlignmentType.LEFT,
          style: { paragraph: { indent: { left: 720, hanging: 360 } } }
        }]
      });
      children.push(...createNumberedList(items, listRef));
      continue;
    }
    
    // 表格
    if (line.includes('|') && i + 1 < lines.length && lines[i + 1].includes('---')) {
      const headers = line.split('|').map(h => h.trim()).filter(h => h);
      i += 2; // 跳过分隔行
      
      const rows = [];
      while (i < lines.length && lines[i].includes('|')) {
        const row = lines[i].split('|').map(c => c.trim()).filter(c => c);
        if (row.length > 0) rows.push(row);
        i++;
      }
      
      if (headers.length > 0 && rows.length > 0) {
        children.push(createTable(headers, rows));
      }
      continue;
    }
    
    // 分页符
    if (line.match(/^---+$/) || line.match(/^\*\*\*+$/)) {
      children.push(new Paragraph({ children: [new PageBreak()] }));
      i++;
      continue;
    }
    
    // 普通段落
    children.push(createParagraph(line));
    i++;
  }
  
  // 如果没有标题但提供了自定义标题
  if (!hasTitle && customTitle) {
    children.unshift(createTitle(customTitle));
  }
  
  return children;
}

// ============ 主函数 ============

async function main() {
  const args = process.argv.slice(2);
  
  // 解析参数
  let inputFile = null;
  let outputFile = 'output.docx';
  let mode = null; // 'json' or 'md'
  let customTitle = null;
  
  for (let i = 0; i < args.length; i++) {
    switch (args[i]) {
      case '--from-json':
        mode = 'json';
        inputFile = args[++i];
        break;
      case '--from-md':
        mode = 'md';
        inputFile = args[++i];
        break;
      case '-o':
      case '--output':
        outputFile = args[++i];
        break;
      case '--title':
        customTitle = args[++i];
        break;
      case '-h':
      case '--help':
        printHelp();
        process.exit(0);
    }
  }
  
  if (!inputFile || !mode) {
    printHelp();
    process.exit(1);
  }
  
  // 检查输入文件
  if (!fs.existsSync(inputFile)) {
    console.error(`❌ 文件不存在: ${inputFile}`);
    process.exit(1);
  }
  
  // 读取并解析输入
  const content = fs.readFileSync(inputFile, 'utf-8');
  let children;
  
  if (mode === 'json') {
    const json = JSON.parse(content);
    children = parseJsonContent(json);
  } else {
    children = parseMarkdown(content, customTitle);
  }
  
  // 创建文档
  const doc = new Document({
    styles: STYLES,
    numbering: NUMBERING,
    sections: [{
      properties: {
        page: { margin: { top: 1440, right: 1440, bottom: 1440, left: 1440 } }
      },
      children: children
    }]
  });
  
  // 保存文档
  const buffer = await Packer.toBuffer(doc);
  fs.writeFileSync(outputFile, buffer);
  
  console.log(`✅ 文档已生成: ${outputFile}`);
}

function printHelp() {
  console.log(`
create_docx.js - 创建格式精美的 Word 文档

使用方法:
  node create_docx.js --from-json <JSON文件> -o <输出文件>
  node create_docx.js --from-md <Markdown文件> -o <输出文件>
  node create_docx.js --from-md <Markdown文件> --title "自定义标题" -o <输出文件>

选项:
  --from-json <文件>    从 JSON 文件创建文档
  --from-md <文件>      从 Markdown 文件创建文档
  --title <标题>        自定义文档标题（仅 Markdown 模式）
  -o, --output <文件>   输出文件路径（默认: output.docx）
  -h, --help           显示帮助信息

JSON 格式示例:
{
  "title": "文档标题",
  "sections": [
    { "type": "heading1", "text": "第一章" },
    { "type": "paragraph", "text": "正文内容，支持 **加粗** 和 *斜体*" },
    { "type": "bullet", "items": ["项目1", "项目2"] },
    { "type": "numbered", "items": ["步骤1", "步骤2"] },
    { "type": "table", "headers": ["列1", "列2"], "rows": [["A", "B"]] },
    { "type": "pagebreak" }
  ]
}

Markdown 支持:
  - # 标题 (H1 作为文档标题)
  - ## 二级标题, ### 三级标题
  - 正文段落
  - - 无序列表 / * 无序列表
  - 1. 有序列表
  - | 表格 | 语法 |
  - --- 分页符
  - **加粗** *斜体*
`);
}

main().catch(err => {
  console.error('❌ 错误:', err.message);
  process.exit(1);
});

