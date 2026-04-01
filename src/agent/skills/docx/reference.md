# DOCX Processing Advanced Reference

本文档包含高级 DOCX 处理功能、详细示例和主要指南中未涵盖的附加库。

## 目录

- [高级 python-docx 用法](#高级-python-docx-用法)
- [docx-js 高级功能](#docx-js-高级功能)
- [docxcompose 文档合并](#docxcompose-文档合并)
- [docx2pdf 转换库](#docx2pdf-转换库)
- [复杂工作流](#复杂工作流)
- [批量处理](#批量处理)
- [性能优化](#性能优化)
- [故障排除](#故障排除)

---

## 高级 python-docx 用法

### 自定义样式

```python
from docx import Document
from docx.shared import Pt, RGBColor, Inches
from docx.enum.style import WD_STYLE_TYPE
from docx.enum.text import WD_ALIGN_PARAGRAPH

doc = Document()

# 创建自定义段落样式
styles = doc.styles
custom_style = styles.add_style('CustomHeading', WD_STYLE_TYPE.PARAGRAPH)
custom_style.base_style = styles['Normal']

# 设置样式属性
custom_font = custom_style.font
custom_font.name = 'Arial'
custom_font.size = Pt(16)
custom_font.bold = True
custom_font.color.rgb = RGBColor(0x2E, 0x74, 0xB5)

custom_format = custom_style.paragraph_format
custom_format.alignment = WD_ALIGN_PARAGRAPH.CENTER
custom_format.space_before = Pt(12)
custom_format.space_after = Pt(6)

# 使用自定义样式
doc.add_paragraph('使用自定义样式的标题', style='CustomHeading')
```

### 复杂表格操作

```python
from docx import Document
from docx.shared import Inches, Pt, RGBColor
from docx.oxml.ns import nsdecls
from docx.oxml import parse_xml
from docx.enum.table import WD_TABLE_ALIGNMENT, WD_CELL_VERTICAL_ALIGNMENT

doc = Document()

# 创建表格
table = doc.add_table(rows=4, cols=4)
table.style = 'Table Grid'
table.alignment = WD_TABLE_ALIGNMENT.CENTER

# 设置列宽
for i, column in enumerate(table.columns):
    for cell in column.cells:
        cell.width = Inches(1.5)

# 合并单元格
table.cell(0, 0).merge(table.cell(0, 1))  # 合并第一行前两个单元格
table.cell(0, 2).merge(table.cell(0, 3))  # 合并第一行后两个单元格

# 设置单元格背景色
def set_cell_background(cell, color):
    """设置单元格背景颜色"""
    shading_elm = parse_xml(
        f'<w:shd {nsdecls("w")} w:fill="{color}"/>'
    )
    cell._tc.get_or_add_tcPr().append(shading_elm)

# 设置表头背景
for cell in table.rows[0].cells:
    set_cell_background(cell, "2E74B5")
    cell.paragraphs[0].runs[0].font.color.rgb = RGBColor(255, 255, 255)

# 垂直居中
for row in table.rows:
    for cell in row.cells:
        cell.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.CENTER

# 填充数据
headers = ['项目', '数量', '单价', '总价']
for i, header in enumerate(headers):
    table.cell(0, i if i < 2 else i).paragraphs[0].add_run(header)

data = [
    ['产品A', '10', '¥100', '¥1,000'],
    ['产品B', '5', '¥200', '¥1,000'],
    ['合计', '', '', '¥2,000'],
]

for row_idx, row_data in enumerate(data):
    for col_idx, cell_text in enumerate(row_data):
        table.cell(row_idx + 1, col_idx).text = cell_text

doc.save('complex_table.docx')
```

### 处理嵌套表格

```python
from docx import Document
from docx.shared import Inches

def add_nested_table(cell, rows, cols):
    """在单元格中添加嵌套表格"""
    # 获取单元格的第一个段落
    paragraph = cell.paragraphs[0]
    
    # 创建嵌套表格
    nested_table = cell.add_table(rows=rows, cols=cols)
    nested_table.style = 'Table Grid'
    
    return nested_table

doc = Document()
main_table = doc.add_table(rows=2, cols=2)
main_table.style = 'Table Grid'

# 在第一个单元格中添加嵌套表格
nested = add_nested_table(main_table.cell(0, 0), 2, 2)
nested.cell(0, 0).text = '嵌套1-1'
nested.cell(0, 1).text = '嵌套1-2'
nested.cell(1, 0).text = '嵌套2-1'
nested.cell(1, 1).text = '嵌套2-2'

doc.save('nested_table.docx')
```

### 超链接和书签

```python
from docx import Document
from docx.oxml.ns import qn
from docx.oxml import OxmlElement

def add_hyperlink(paragraph, url, text, color='0000FF'):
    """添加超链接"""
    part = paragraph.part
    r_id = part.relate_to(
        url,
        'http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink',
        is_external=True
    )
    
    hyperlink = OxmlElement('w:hyperlink')
    hyperlink.set(qn('r:id'), r_id)
    
    new_run = OxmlElement('w:r')
    rPr = OxmlElement('w:rPr')
    
    # 设置颜色
    c = OxmlElement('w:color')
    c.set(qn('w:val'), color)
    rPr.append(c)
    
    # 设置下划线
    u = OxmlElement('w:u')
    u.set(qn('w:val'), 'single')
    rPr.append(u)
    
    new_run.append(rPr)
    new_run.text = text
    hyperlink.append(new_run)
    
    paragraph._p.append(hyperlink)
    return hyperlink

doc = Document()
para = doc.add_paragraph('访问我们的网站: ')
add_hyperlink(para, 'https://www.example.com', '点击这里')

doc.save('hyperlink.docx')
```

### 添加目录

```python
from docx import Document
from docx.oxml.ns import qn
from docx.oxml import OxmlElement

def add_toc(doc):
    """添加目录（需要在Word中更新）"""
    paragraph = doc.add_paragraph()
    run = paragraph.add_run()
    
    fldChar1 = OxmlElement('w:fldChar')
    fldChar1.set(qn('w:fldCharType'), 'begin')
    
    instrText = OxmlElement('w:instrText')
    instrText.set(qn('xml:space'), 'preserve')
    instrText.text = 'TOC \\o "1-3" \\h \\z \\u'
    
    fldChar2 = OxmlElement('w:fldChar')
    fldChar2.set(qn('w:fldCharType'), 'separate')
    
    fldChar3 = OxmlElement('w:fldChar')
    fldChar3.set(qn('w:fldCharType'), 'end')
    
    run._r.append(fldChar1)
    run._r.append(instrText)
    run._r.append(fldChar2)
    run._r.append(fldChar3)
    
    return paragraph

doc = Document()
doc.add_heading('文档标题', 0)
add_toc(doc)
doc.add_page_break()

# 添加内容
doc.add_heading('第一章', 1)
doc.add_paragraph('第一章内容...')
doc.add_heading('1.1 小节', 2)
doc.add_paragraph('小节内容...')

doc.add_heading('第二章', 1)
doc.add_paragraph('第二章内容...')

doc.save('with_toc.docx')
print("提示: 请在Word中按Ctrl+A后按F9更新目录")
```

### 水印添加

```python
from docx import Document
from docx.oxml.ns import qn
from docx.oxml import OxmlElement

def add_watermark(doc, text):
    """添加文字水印"""
    for section in doc.sections:
        header = section.header
        paragraph = header.paragraphs[0] if header.paragraphs else header.add_paragraph()
        
        # 创建水印形状
        run = paragraph.add_run()
        
        pict = OxmlElement('w:pict')
        shape = OxmlElement('v:shape')
        shape.set('type', '#_x0000_t136')  # WordArt 类型
        shape.set('style', 
            'position:absolute;'
            'margin-left:0;margin-top:0;'
            'width:400pt;height:100pt;'
            'rotation:-45;'
            'z-index:-251658752;'
        )
        shape.set('fillcolor', 'silver')
        shape.set('stroked', 'f')
        
        textpath = OxmlElement('v:textpath')
        textpath.set('style', 'font-family:"Arial";font-size:60pt')
        textpath.set('string', text)
        
        shape.append(textpath)
        pict.append(shape)
        run._r.append(pict)

doc = Document('input.docx')
add_watermark(doc, '机密文件')
doc.save('watermarked.docx')
```

---

## docx-js 高级功能

### 创建带图表的文档

```javascript
const { Document, Packer, Paragraph, TextRun, Table, TableRow, TableCell,
        ImageRun, HeadingLevel, AlignmentType, BorderStyle } = require('docx');
const fs = require('fs');

async function createReport() {
    // 读取图表图片
    const chartImage = fs.readFileSync('chart.png');
    
    const doc = new Document({
        sections: [{
            properties: {
                page: {
                    margin: { top: 720, right: 720, bottom: 720, left: 720 }
                }
            },
            children: [
                new Paragraph({
                    heading: HeadingLevel.TITLE,
                    children: [new TextRun({ text: "季度销售报告", bold: true })]
                }),
                
                new Paragraph({
                    children: [new TextRun("以下是本季度销售数据分析:")]
                }),
                
                // 添加图表
                new Paragraph({
                    alignment: AlignmentType.CENTER,
                    children: [
                        new ImageRun({
                            type: 'png',
                            data: chartImage,
                            transformation: { width: 500, height: 300 }
                        })
                    ]
                }),
                
                // 添加数据表
                createSalesTable(),
                
                new Paragraph({
                    heading: HeadingLevel.HEADING_1,
                    children: [new TextRun("结论")]
                }),
                
                new Paragraph({
                    children: [new TextRun("本季度销售额较上季度增长15%...")]
                })
            ]
        }]
    });
    
    const buffer = await Packer.toBuffer(doc);
    fs.writeFileSync('report.docx', buffer);
    console.log('报告已生成');
}

function createSalesTable() {
    const data = [
        ['月份', '销售额', '增长率'],
        ['1月', '¥100,000', '+5%'],
        ['2月', '¥120,000', '+20%'],
        ['3月', '¥115,000', '-4%']
    ];
    
    return new Table({
        columnWidths: [3000, 3000, 3000],
        rows: data.map((row, rowIndex) => 
            new TableRow({
                children: row.map(cell =>
                    new TableCell({
                        children: [new Paragraph({
                            alignment: AlignmentType.CENTER,
                            children: [new TextRun({
                                text: cell,
                                bold: rowIndex === 0
                            })]
                        })],
                        shading: rowIndex === 0 ? { fill: "2E74B5" } : undefined
                    })
                )
            })
        )
    });
}

createReport();
```

### 多节文档（不同页面设置）

```javascript
const { Document, Packer, Paragraph, TextRun, PageOrientation,
        Header, Footer, PageNumber, AlignmentType } = require('docx');
const fs = require('fs');

const doc = new Document({
    sections: [
        // 第一节: 竖向，带标题页
        {
            properties: {
                page: {
                    margin: { top: 1440, right: 1440, bottom: 1440, left: 1440 }
                }
            },
            children: [
                new Paragraph({
                    alignment: AlignmentType.CENTER,
                    spacing: { before: 3000 },
                    children: [new TextRun({ text: "项目报告", size: 72, bold: true })]
                }),
                new Paragraph({
                    alignment: AlignmentType.CENTER,
                    spacing: { before: 500 },
                    children: [new TextRun({ text: "2025年度", size: 36 })]
                })
            ]
        },
        
        // 第二节: 竖向，正文内容
        {
            properties: {
                page: {
                    margin: { top: 1440, right: 1440, bottom: 1440, left: 1440 }
                }
            },
            headers: {
                default: new Header({
                    children: [new Paragraph({
                        alignment: AlignmentType.RIGHT,
                        children: [new TextRun("项目报告 - 正文")]
                    })]
                })
            },
            footers: {
                default: new Footer({
                    children: [new Paragraph({
                        alignment: AlignmentType.CENTER,
                        children: [
                            new TextRun("第 "),
                            new TextRun({ children: [PageNumber.CURRENT] }),
                            new TextRun(" 页")
                        ]
                    })]
                })
            },
            children: [
                new Paragraph({
                    children: [new TextRun("第一章 项目概述")]
                }),
                new Paragraph({
                    children: [new TextRun("项目内容描述...")]
                })
            ]
        },
        
        // 第三节: 横向，用于宽表格
        {
            properties: {
                page: {
                    size: { orientation: PageOrientation.LANDSCAPE },
                    margin: { top: 720, right: 720, bottom: 720, left: 720 }
                }
            },
            children: [
                new Paragraph({
                    children: [new TextRun("附录: 数据表格")]
                }),
                // 添加宽表格...
            ]
        }
    ]
});

Packer.toBuffer(doc).then(buffer => {
    fs.writeFileSync('multi_section.docx', buffer);
});
```

---

## docxcompose 文档合并

### 高级合并选项

```python
from docxcompose.composer import Composer
from docx import Document

def advanced_merge(output_path, doc_paths, add_page_breaks=True):
    """高级文档合并，支持分页控制"""
    if not doc_paths:
        raise ValueError("至少需要一个文档")
    
    # 使用第一个文档作为主文档
    master = Document(doc_paths[0])
    composer = Composer(master)
    
    # 追加其他文档
    for doc_path in doc_paths[1:]:
        doc = Document(doc_path)
        
        if add_page_breaks:
            # 添加分页符
            master.add_page_break()
        
        composer.append(doc)
    
    composer.save(output_path)
    print(f"已合并 {len(doc_paths)} 个文档到 {output_path}")

# 使用示例
doc_list = ['chapter1.docx', 'chapter2.docx', 'chapter3.docx']
advanced_merge('complete_book.docx', doc_list)
```

### 保留样式的合并

```python
from docxcompose.composer import Composer
from docx import Document

def merge_with_style_handling(output_path, doc_paths):
    """合并文档并处理样式冲突"""
    master = Document(doc_paths[0])
    
    # 保存主文档的样式信息
    master_styles = set(style.name for style in master.styles)
    
    composer = Composer(master)
    
    for doc_path in doc_paths[1:]:
        doc = Document(doc_path)
        
        # 检查样式冲突
        for style in doc.styles:
            if style.name in master_styles:
                print(f"警告: 样式 '{style.name}' 在 {doc_path} 中可能被覆盖")
        
        composer.append(doc)
    
    composer.save(output_path)

# 合并同时检查样式
merge_with_style_handling('merged.docx', ['doc1.docx', 'doc2.docx'])
```

---

## docx2pdf 转换库

### 使用 docx2pdf（仅Windows/macOS）

```python
# pip install docx2pdf
from docx2pdf import convert

# 单文件转换
convert("document.docx")  # 生成 document.pdf

# 指定输出路径
convert("document.docx", "output.pdf")

# 批量转换目录
convert("./input_folder/", "./output_folder/")

# 保持目录结构
convert("./docs/", "./pdfs/", keep_active=True)
```

### 跨平台转换方案

```python
import subprocess
import platform
import os

def convert_to_pdf(docx_path, output_path=None):
    """跨平台 DOCX 转 PDF"""
    if not os.path.exists(docx_path):
        raise FileNotFoundError(f"文件不存在: {docx_path}")
    
    if output_path is None:
        output_path = os.path.splitext(docx_path)[0] + '.pdf'
    
    output_dir = os.path.dirname(output_path) or '.'
    
    system = platform.system()
    
    if system == 'Windows':
        # Windows: 尝试 docx2pdf，失败则用 LibreOffice
        try:
            from docx2pdf import convert
            convert(docx_path, output_path)
        except ImportError:
            subprocess.run([
                'soffice', '--headless', '--convert-to', 'pdf',
                '--outdir', output_dir, docx_path
            ], check=True)
    
    elif system == 'Darwin':  # macOS
        try:
            from docx2pdf import convert
            convert(docx_path, output_path)
        except ImportError:
            subprocess.run([
                '/Applications/LibreOffice.app/Contents/MacOS/soffice',
                '--headless', '--convert-to', 'pdf',
                '--outdir', output_dir, docx_path
            ], check=True)
    
    else:  # Linux
        subprocess.run([
            'soffice', '--headless', '--convert-to', 'pdf',
            '--outdir', output_dir, docx_path
        ], check=True)
    
    return output_path

# 使用
pdf_path = convert_to_pdf('document.docx')
print(f"已转换: {pdf_path}")
```

---

## 复杂工作流

### 文档比较

```python
from docx import Document
from difflib import unified_diff

def compare_documents(doc1_path, doc2_path):
    """比较两个文档的文本差异"""
    doc1 = Document(doc1_path)
    doc2 = Document(doc2_path)
    
    # 提取文本
    text1 = [p.text for p in doc1.paragraphs if p.text.strip()]
    text2 = [p.text for p in doc2.paragraphs if p.text.strip()]
    
    # 计算差异
    diff = list(unified_diff(
        text1, text2,
        fromfile=doc1_path,
        tofile=doc2_path,
        lineterm=''
    ))
    
    if diff:
        print("文档差异:")
        for line in diff:
            print(line)
    else:
        print("两个文档内容相同")
    
    return diff

# 使用
compare_documents('version1.docx', 'version2.docx')
```

### 提取所有评论

```python
from docx import Document
from lxml import etree

def extract_comments(doc_path):
    """提取文档中的所有评论"""
    from zipfile import ZipFile
    import xml.etree.ElementTree as ET
    
    comments = []
    
    with ZipFile(doc_path, 'r') as zip_file:
        # 检查是否存在评论文件
        if 'word/comments.xml' not in zip_file.namelist():
            return comments
        
        # 解析评论 XML
        with zip_file.open('word/comments.xml') as f:
            tree = ET.parse(f)
            root = tree.getroot()
            
            # 命名空间
            ns = {
                'w': 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'
            }
            
            for comment in root.findall('.//w:comment', ns):
                comment_id = comment.get('{http://schemas.openxmlformats.org/wordprocessingml/2006/main}id')
                author = comment.get('{http://schemas.openxmlformats.org/wordprocessingml/2006/main}author')
                date = comment.get('{http://schemas.openxmlformats.org/wordprocessingml/2006/main}date')
                
                # 提取评论文本
                text_elements = comment.findall('.//w:t', ns)
                text = ''.join(t.text or '' for t in text_elements)
                
                comments.append({
                    'id': comment_id,
                    'author': author,
                    'date': date,
                    'text': text
                })
    
    return comments

# 使用
comments = extract_comments('reviewed_document.docx')
for c in comments:
    print(f"[{c['author']}] {c['date']}: {c['text']}")
```

### 文档加密检测

```python
from zipfile import ZipFile, BadZipFile
from docx import Document

def check_document_encryption(doc_path):
    """检查文档是否加密"""
    try:
        with ZipFile(doc_path, 'r') as zip_file:
            # 尝试读取主要内容
            if 'word/document.xml' in zip_file.namelist():
                # 尝试解析
                doc = Document(doc_path)
                return {'encrypted': False, 'readable': True}
            else:
                return {'encrypted': True, 'readable': False, 
                        'message': '文档可能被加密或损坏'}
    except BadZipFile:
        return {'encrypted': True, 'readable': False,
                'message': '无法打开文件，可能被加密'}
    except Exception as e:
        return {'encrypted': None, 'readable': False,
                'message': f'检查失败: {str(e)}'}

# 使用
result = check_document_encryption('document.docx')
print(result)
```

---

## 批量处理

### 批量转换目录

```python
import os
from pathlib import Path
from docx import Document
import subprocess

def batch_convert_to_pdf(input_dir, output_dir):
    """批量将目录下的 DOCX 转换为 PDF"""
    input_path = Path(input_dir)
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    
    docx_files = list(input_path.glob('**/*.docx'))
    
    for i, docx_file in enumerate(docx_files):
        # 保持目录结构
        relative_path = docx_file.relative_to(input_path)
        pdf_dir = output_path / relative_path.parent
        pdf_dir.mkdir(parents=True, exist_ok=True)
        
        print(f"[{i+1}/{len(docx_files)}] 转换: {docx_file.name}")
        
        try:
            subprocess.run([
                'soffice', '--headless', '--convert-to', 'pdf',
                '--outdir', str(pdf_dir), str(docx_file)
            ], check=True, capture_output=True)
        except subprocess.CalledProcessError as e:
            print(f"  转换失败: {e}")
    
    print(f"完成! 共转换 {len(docx_files)} 个文件")

# 使用
batch_convert_to_pdf('./documents', './pdf_output')
```

### 批量提取文本

```python
import os
import json
from pathlib import Path
from docx import Document

def batch_extract_text(input_dir, output_file):
    """批量提取目录下所有 DOCX 的文本"""
    input_path = Path(input_dir)
    results = []
    
    for docx_file in input_path.glob('**/*.docx'):
        try:
            doc = Document(str(docx_file))
            text = '\n'.join(p.text for p in doc.paragraphs if p.text.strip())
            
            results.append({
                'file': str(docx_file),
                'text': text,
                'word_count': len(text.split()),
                'paragraph_count': len(doc.paragraphs)
            })
            print(f"已处理: {docx_file.name}")
        except Exception as e:
            results.append({
                'file': str(docx_file),
                'error': str(e)
            })
            print(f"处理失败: {docx_file.name} - {e}")
    
    # 保存结果
    with open(output_file, 'w', encoding='utf-8') as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    
    print(f"结果已保存到: {output_file}")

# 使用
batch_extract_text('./documents', 'extracted_texts.json')
```

### 批量替换文本

```python
import os
from pathlib import Path
from docx import Document

def batch_replace_text(input_dir, output_dir, replacements):
    """批量替换目录下所有 DOCX 中的文本"""
    input_path = Path(input_dir)
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    
    for docx_file in input_path.glob('*.docx'):
        doc = Document(str(docx_file))
        modified = False
        
        # 替换段落中的文本
        for para in doc.paragraphs:
            for old_text, new_text in replacements.items():
                if old_text in para.text:
                    for run in para.runs:
                        if old_text in run.text:
                            run.text = run.text.replace(old_text, new_text)
                            modified = True
        
        # 替换表格中的文本
        for table in doc.tables:
            for row in table.rows:
                for cell in row.cells:
                    for para in cell.paragraphs:
                        for old_text, new_text in replacements.items():
                            if old_text in para.text:
                                for run in para.runs:
                                    if old_text in run.text:
                                        run.text = run.text.replace(old_text, new_text)
                                        modified = True
        
        # 保存
        output_file = output_path / docx_file.name
        doc.save(str(output_file))
        
        status = "已修改" if modified else "无变化"
        print(f"{docx_file.name}: {status}")

# 使用
replacements = {
    '旧公司名': '新公司名',
    '2024': '2025',
    'old@email.com': 'new@email.com'
}
batch_replace_text('./input', './output', replacements)
```

---

## 性能优化

### 大文件处理

```python
from docx import Document
import gc

def process_large_document(doc_path, chunk_size=100):
    """分块处理大型文档"""
    doc = Document(doc_path)
    paragraphs = doc.paragraphs
    total = len(paragraphs)
    
    results = []
    
    for i in range(0, total, chunk_size):
        chunk = paragraphs[i:i + chunk_size]
        
        # 处理这批段落
        for para in chunk:
            # 你的处理逻辑
            text = para.text
            if text.strip():
                results.append(text)
        
        # 定期清理内存
        if i % (chunk_size * 10) == 0:
            gc.collect()
            print(f"已处理: {min(i + chunk_size, total)}/{total}")
    
    return results

# 使用
texts = process_large_document('large_document.docx')
```

### 并行处理多个文档

```python
import os
from pathlib import Path
from concurrent.futures import ProcessPoolExecutor, as_completed
from docx import Document

def process_single_document(doc_path):
    """处理单个文档（用于并行）"""
    try:
        doc = Document(str(doc_path))
        text = '\n'.join(p.text for p in doc.paragraphs if p.text.strip())
        word_count = len(text.split())
        return {'file': str(doc_path), 'word_count': word_count, 'success': True}
    except Exception as e:
        return {'file': str(doc_path), 'error': str(e), 'success': False}

def parallel_process_documents(input_dir, max_workers=4):
    """并行处理目录下的所有文档"""
    input_path = Path(input_dir)
    doc_files = list(input_path.glob('*.docx'))
    
    results = []
    
    with ProcessPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(process_single_document, f): f for f in doc_files}
        
        for future in as_completed(futures):
            result = future.result()
            results.append(result)
            
            if result['success']:
                print(f"✓ {Path(result['file']).name}: {result['word_count']} 字")
            else:
                print(f"✗ {Path(result['file']).name}: {result['error']}")
    
    return results

# 使用
results = parallel_process_documents('./documents', max_workers=4)
```

---

## 故障排除

### 常见错误和解决方案

#### 1. PackageNotFoundError: Package not found

```python
# 错误: Package not found at 'document.docx'

# 解决: 检查文件路径是否正确
from pathlib import Path

doc_path = Path('document.docx')
if not doc_path.exists():
    print(f"文件不存在: {doc_path.absolute()}")
else:
    doc = Document(str(doc_path))
```

#### 2. KeyError: 访问不存在的样式

```python
# 错误: KeyError: "There is no style called 'CustomStyle'"

# 解决: 检查样式是否存在
doc = Document('document.docx')
available_styles = [s.name for s in doc.styles]
print("可用样式:", available_styles)

# 安全使用样式
style_name = 'CustomStyle'
if style_name in available_styles:
    doc.add_paragraph('Text', style=style_name)
else:
    doc.add_paragraph('Text')  # 使用默认样式
```

#### 3. 编码问题

```python
# 错误: UnicodeDecodeError

# 解决: 确保正确的编码
import codecs

def safe_read_docx(doc_path):
    try:
        return Document(doc_path)
    except Exception as e:
        print(f"读取失败: {e}")
        return None

# 保存时确保 UTF-8
doc = Document()
doc.add_paragraph('中文内容')
doc.save('output.docx')  # python-docx 默认使用 UTF-8
```

#### 4. 内存不足

```python
# 处理大文件时的内存优化
import gc

def memory_efficient_processing(doc_path):
    doc = Document(doc_path)
    
    # 逐段处理，避免一次性加载所有内容
    for para in doc.paragraphs:
        text = para.text
        # 处理文本...
        del text  # 显式释放
    
    gc.collect()
    del doc
    gc.collect()
```

### 调试技巧

```python
from docx import Document
from docx.oxml import etree

def debug_document_structure(doc_path):
    """调试文档结构"""
    doc = Document(doc_path)
    
    print("=== 文档结构 ===")
    print(f"段落数: {len(doc.paragraphs)}")
    print(f"表格数: {len(doc.tables)}")
    print(f"节数: {len(doc.sections)}")
    
    print("\n=== 样式列表 ===")
    for style in doc.styles[:10]:  # 前10个样式
        print(f"  - {style.name} ({style.type})")
    
    print("\n=== 段落详情 ===")
    for i, para in enumerate(doc.paragraphs[:5]):  # 前5个段落
        print(f"  [{i}] 样式: {para.style.name}, 文本: {para.text[:50]}...")
    
    print("\n=== XML 结构（部分）===")
    body = doc._body._body
    xml_str = etree.tostring(body, pretty_print=True, encoding='unicode')
    print(xml_str[:2000])  # 前2000字符

# 使用
debug_document_structure('document.docx')
```

---

## 许可证信息

- **python-docx**: MIT License
- **docxcompose**: MIT License
- **docx (JS)**: MIT License
- **docx2pdf**: MIT License
- **LibreOffice**: MPL 2.0

