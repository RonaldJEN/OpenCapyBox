---
name: docx
description: "Word文档(.docx)的创建、编辑和读取。支持: (1) 创建新文档, (2) 修改编辑现有文档, (3) 读取提取内容, (4) 合并拆分文档, (5) 模板填充"
license: Proprietary. LICENSE.txt has complete terms
---

# DOCX 文档操作指南

## Overview

处理 Word 文档(.docx)的五种场景：**读取**、**创建**、**编辑**、**合并拆分**、**模板填充**。

### ⚠️ 创建新文档选择指南

| 场景 | 推荐方案 | 原因 |
|------|----------|------|
| **Markdown 转 DOCX** | ✅ docx-js (JavaScript) | 格式精美、支持完整样式层次 |
| **创建新文档** | ✅ docx-js (JavaScript) | 专业排版、标题/列表/表格格式正确 |
| **修改现有文档** | python-docx (Python) | 保留原格式，只改内容 |

> 💡 **为什么推荐 docx-js？** python-docx 创建的文档格式较简陋，而 docx-js 支持专业字体、样式、TOC 等，生成的文档开箱即用，无需在 Word 中手动调整格式。

对于高级功能和复杂工作流，请参阅 [reference.md](reference.md)。

## 重要提示

⚠️ **文件路径处理规则**：
- 文件名包含**空格**或**中文**时，**必须**用双引号包裹
- 脚本路径: `skills/document-skills/docx/scripts/xxx.py`
- 示例: `python skills/document-skills/docx/scripts/read_docx.py "我的文档 v2.docx"`

## Quick Start

### 读取和编辑（Python）
```bash
# 读取文档内容（支持嵌套表格）
python skills/document-skills/docx/scripts/read_docx.py document.docx

# 查看文档结构
python skills/document-skills/docx/scripts/read_docx.py document.docx --structure

# 文本替换
python skills/document-skills/docx/scripts/edit_docx.py doc.docx --replace "旧文本" "新文本" -o output.docx

# 表格数据更新
python skills/document-skills/docx/scripts/update_table.py doc.docx "T1" --cell 0,0 "新值" -o output.docx
```

### 创建新文档 / MD转DOCX - create_docx.js（推荐）

```bash
# Markdown 转 DOCX（最常用）
node skills/document-skills/docx/scripts/create_docx.js --from-md "文档.md" -o output.docx

# 自定义标题
node skills/document-skills/docx/scripts/create_docx.js --from-md "README.md" --title "项目说明" -o output.docx

# 从 JSON 创建（适合程序化生成）
node skills/document-skills/docx/scripts/create_docx.js --from-json content.json -o output.docx
```

**JSON 格式示例**（content.json）：
```json
{
  "title": "文档标题",
  "sections": [
    { "type": "heading1", "text": "第一章" },
    { "type": "paragraph", "text": "正文内容，支持 **加粗** 和 *斜体*" },
    { "type": "bullet", "items": ["项目1", "项目2", "项目3"] },
    { "type": "heading2", "text": "1.1 小节" },
    { "type": "numbered", "items": ["步骤1", "步骤2"] },
    { "type": "table", "headers": ["列1", "列2"], "rows": [["A", "B"], ["C", "D"]] },
    { "type": "pagebreak" }
  ]
}
```

**支持的 Markdown 语法**：
- `# 标题` → 文档标题（首个 H1）
- `## 二级标题` / `### 三级标题`
- `- 列表项` / `* 列表项` → 无序列表
- `1. 编号项` → 有序列表
- `| 表格 | 语法 |` → 表格
- `---` → 分页符
- `**加粗**` / `*斜体*` → 文本格式

> 📖 如需更复杂的自定义，参见 [docx-js.md](docx-js.md)

## 脚本使用指南

### 读取文档 - read_docx.py

**命令格式**:
```bash
python skills/document-skills/docx/scripts/read_docx.py <DOCX文件路径> [选项]
```

**选项**:
- `--structure` - 显示文档结构（含嵌套表格统计）
- `--images <目录>` - 提取图片到指定目录

**示例**:
```bash
# 读取文档内容
python skills/document-skills/docx/scripts/read_docx.py 报告.docx

# 显示文档结构
python skills/document-skills/docx/scripts/read_docx.py 报告.docx --structure

# 提取图片
python skills/document-skills/docx/scripts/read_docx.py 报告.docx --images ./img
```

**嵌套表格输出示例**:
```
[T1] (3行 x 2列)
  | 项目 | 详情 |
  ↳ 单元格[0,1]包含嵌套表格:
    [T1.C[0,1].T1] (2行 x 3列)
      | 日期 | 金额 | 备注 |
      | 2025-01-01 | ¥1000 | 首付款 |
  | 合计 | ¥5000 |
```

### 编辑文档 - edit_docx.py（通用文本替换）

**命令格式**:
```bash
python skills/document-skills/docx/scripts/edit_docx.py <DOCX文件> [选项] -o <输出文件>
```

**选项**:
- `--replace "旧文本" "新文本"` - 替换文本（可多次使用）
- `--replacements <JSON>` - 从JSON文件/字符串批量替换
- `--regex "模式" "替换"` - 正则表达式替换
- `--dry-run` - 预览模式，不保存

**示例**:
```bash
# 简单替换
python skills/document-skills/docx/scripts/edit_docx.py template.docx --replace "借款人" "被投资人xxx" -o filled.docx

# 多个替换
python skills/document-skills/docx/scripts/edit_docx.py template.docx \
    --replace "借款人" "被投资人广东文科绿色科技股份有限公司" \
    --replace "保证人" "收购人佛山市建设发展集团有限公司" \
    -o output.docx

# 从JSON文件批量替换
# replacements.json: {"借款人": "被投资人xxx", "保证人": "收购人yyy"}
python skills/document-skills/docx/scripts/edit_docx.py template.docx --replacements replacements.json -o output.docx

# 正则替换（如日期格式）
python skills/document-skills/docx/scripts/edit_docx.py doc.docx --regex "\\d{4}-\\d{2}-\\d{2}" "2025-01-01" -o output.docx

# 预览更改
python skills/document-skills/docx/scripts/edit_docx.py doc.docx --replace "旧" "新" --dry-run
```

### 表格数据更新 - update_table.py

**命令格式**:
```bash
python skills/document-skills/docx/scripts/update_table.py <DOCX文件> <表格路径> [选项] -o <输出文件>
```

**表格路径格式**:
| 路径 | 含义 |
|------|------|
| `T1` | 文档第1个顶层表格 |
| `T1.C[0,1].T1` | T1表格的单元格[0,1]中的第1个嵌套表格 |
| `T1.C[0,1].T1.C[2,0].T1` | 更深层嵌套 |

**选项**:
- `--list` - 列出所有表格路径
- `--cell ROW,COL VALUE` - 更新指定单元格
- `--row INDEX JSON_ARRAY` - 更新整行
- `--data JSON` - 从JSON文件/字符串批量更新
- `--dry-run` - 预览目标表格

**示例**:
```bash
# 列出所有表格（发现表格路径）
python skills/document-skills/docx/scripts/update_table.py document.docx --list

# 更新单个单元格
python skills/document-skills/docx/scripts/update_table.py doc.docx "T1" --cell 0,0 "公司名称" -o output.docx

# 更新多个单元格
python skills/document-skills/docx/scripts/update_table.py doc.docx "T1" \
    --cell 1,1 "10.25亿" \
    --cell 1,2 "6.95亿" \
    --cell 2,1 "93.92%" \
    -o output.docx

# 更新嵌套表格
python skills/document-skills/docx/scripts/update_table.py doc.docx "T1.C[4,1].T1" \
    --cell 0,0 "2023年" --cell 0,1 "2024年" \
    -o output.docx

# 更新整行
python skills/document-skills/docx/scripts/update_table.py doc.docx "T1" \
    --row 2 '["营业收入", "10.25亿", "6.95亿", "4.10亿"]' \
    -o output.docx

# 从JSON批量更新
# table_data.json 格式：
# {
#   "cells": {"1,1": "值1", "2,3": "值2"},
#   "rows": {"0": ["A", "B", "C"]}
# }
python skills/document-skills/docx/scripts/update_table.py doc.docx "T1.C[4,1].T1" --data table_data.json -o output.docx

# 预览表格内容
python skills/document-skills/docx/scripts/update_table.py doc.docx "T1.C[4,1].T1" --dry-run
```

### 合并文档 - merge_docx.py

**命令格式**:
```bash
python skills/document-skills/docx/scripts/merge_docx.py <文件1> <文件2> [...] -o <输出文件>
```

**示例**:
```bash
python skills/document-skills/docx/scripts/merge_docx.py doc1.docx doc2.docx -o merged.docx
```

### 填充模板 - fill_template.py

使用 `{{placeholder}}` 格式的占位符进行模板填充。

**命令格式**:
```bash
python skills/document-skills/docx/scripts/fill_template.py <模板文件> <JSON数据> -o <输出文件>
```

**示例**:
```bash
python skills/document-skills/docx/scripts/fill_template.py template.docx data.json -o output.docx
```

### 转换文档 - convert_docx.py

**命令格式**:
```bash
python skills/document-skills/docx/scripts/convert_docx.py <DOCX文件> --to <格式>
```

**示例**:
```bash
# 转PDF（需要LibreOffice）
python skills/document-skills/docx/scripts/convert_docx.py document.docx --to pdf

# 转HTML
python skills/document-skills/docx/scripts/convert_docx.py document.docx --to html
```

---

## 常见工作流

### 工作流1：模板文本替换 + 表格数据填充

适用于：基于模板生成报告、替换公司名称并更新财务数据

```bash
# 步骤1：分析文档结构
python skills/document-skills/docx/scripts/read_docx.py template.docx --structure

# 步骤2：文本替换（公司名称等）
python skills/document-skills/docx/scripts/edit_docx.py template.docx \
    --replace "借款人" "被投资人广东文科绿色科技股份有限公司" \
    --replace "保证人" "收购人佛山市建设发展集团有限公司" \
    -o step2_output.docx

# 步骤3：查看表格路径
python skills/document-skills/docx/scripts/update_table.py step2_output.docx --list

# 步骤4：更新表格数据
python skills/document-skills/docx/scripts/update_table.py step2_output.docx "T1.C[4,1].T1" \
    --data financial_data.json \
    -o final_output.docx
```

### 工作流2：批量生成文档

适用于：合同、通知等批量生成

```bash
# 使用 fill_template.py 批量模式
# clients.json: [{"name": "客户A", "amount": "1000"}, {"name": "客户B", "amount": "2000"}]
python skills/document-skills/docx/scripts/fill_template.py template.docx clients.json --batch -o ./contracts/
```

### 工作流3：文档审阅修改

适用于：多处文本修改、统一术语

```bash
# 创建替换规则文件
echo '{"旧术语1": "新术语1", "旧术语2": "新术语2", "公司A": "公司B"}' > replacements.json

# 批量替换
python skills/document-skills/docx/scripts/edit_docx.py document.docx --replacements replacements.json -o revised.docx
```

### 工作流4：Markdown 转 DOCX（推荐 docx-js）

适用于：将 MD 文档转换为格式精美的 Word 文档

**步骤：**
1. 读取 MD 文件内容
2. 解析 MD 结构（标题、列表、表格等）
3. 使用 docx-js 生成对应的 Word 元素
4. 保存为 .docx

**MD 元素到 docx-js 的映射：**
| Markdown | docx-js |
|----------|---------|
| `# 标题` | `new Paragraph({ heading: HeadingLevel.HEADING_1, ... })` |
| `## 标题` | `new Paragraph({ heading: HeadingLevel.HEADING_2, ... })` |
| `- 列表项` | `new Paragraph({ numbering: { reference: "bullet-list", level: 0 }, ... })` |
| `1. 编号` | `new Paragraph({ numbering: { reference: "numbered-list", level: 0 }, ... })` |
| `**粗体**` | `new TextRun({ text: "粗体", bold: true })` |
| `*斜体*` | `new TextRun({ text: "斜体", italics: true })` |
| 表格 | `new Table({ rows: [...] })` |

> 💡 **提示**：先用 `read_file` 读取 MD 内容，解析结构后用 docx-js 模板生成 Word 文档。

---

## Python 直接使用

### 基础读取

```python
from docx import Document

doc = Document('document.docx')

# 提取文本
for para in doc.paragraphs:
    print(para.text)

# 读取表格（包含嵌套表格）
def read_table_recursive(table, level=0):
    indent = "  " * level
    for row in table.rows:
        for cell in row.cells:
            print(f"{indent}{cell.text[:30]}")
            for nested in cell.tables:  # 关键：cell.tables 访问嵌套表格
                read_table_recursive(nested, level + 1)

for table in doc.tables:
    read_table_recursive(table)
```

### 基础编辑

```python
from docx import Document

doc = Document('existing.docx')

# 修改段落
for para in doc.paragraphs:
    for run in para.runs:
        if '旧文本' in run.text:
            run.text = run.text.replace('旧文本', '新文本')

# 修改表格（包括嵌套表格）
def replace_in_table(table, old, new):
    for row in table.rows:
        for cell in row.cells:
            for para in cell.paragraphs:
                for run in para.runs:
                    if old in run.text:
                        run.text = run.text.replace(old, new)
            for nested in cell.tables:
                replace_in_table(nested, old, new)

for table in doc.tables:
    replace_in_table(table, '{{占位符}}', '实际值')

doc.save('modified.docx')
```

### 创建新文档

> ⚠️ **强烈推荐使用 docx-js（JavaScript）创建新文档！**
> 
> python-docx 创建的文档格式简陋（默认字体、无样式层次、列表符号不正确），需要在 Word 中手动调整。
> 
> docx-js 支持：专业字体（Arial）、标题样式层次、正确的项目符号和编号、表格边框和底纹。
> 
> 👉 详见 [docx-js.md](docx-js.md) 或使用 Quick Start 中的模板。

**仅当需要极简文档且不在意格式时**，可用 Python：

```python
from docx import Document
from docx.shared import Inches, Pt

doc = Document()
doc.add_heading('文档标题', level=0)
doc.add_paragraph('这是一个段落。')

# 添加表格
table = doc.add_table(rows=2, cols=2)
table.style = 'Table Grid'
table.cell(0, 0).text = '标题1'
table.cell(0, 1).text = '标题2'

doc.save('new_document.docx')
```

---

## 快速决策

| 任务 | 命令 |
|------|------|
| 读取/提取文本 | `python .../read_docx.py <文件>` |
| 查看文档结构 | `python .../read_docx.py <文件> --structure` |
| **通用文本替换** | `python .../edit_docx.py <文件> --replace "旧" "新" -o <输出>` |
| **表格数据更新** | `python .../update_table.py <文件> "T1" --cell 0,0 "值" -o <输出>` |
| **列出所有表格** | `python .../update_table.py <文件> --list` |
| ⭐ **创建新文档** | `node .../create_docx.js --from-json content.json -o output.docx` |
| ⭐ **Markdown 转 DOCX** | `node .../create_docx.js --from-md doc.md -o output.docx` |
| 合并多个文档 | `python .../merge_docx.py <文件...> -o <输出>` |
| 模板填充 | `python .../fill_template.py <模板> <数据> -o <输出>` |
| DOCX 转 PDF | `python .../convert_docx.py <文件> --to pdf` |

> 💡 脚本路径: `skills/document-skills/docx/scripts/`

## 依赖安装

```bash
pip install python-docx docxcompose
```

## 下一步

- 复杂排版和高级功能，请参阅 [reference.md](reference.md)
- JavaScript 库（docx-js）用法，请参阅 [docx-js.md](docx-js.md)
- OOXML 底层操作，请参阅 [ooxml.md](ooxml.md)
