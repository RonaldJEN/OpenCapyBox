#!/usr/bin/env python3
"""
Simple Word document reader using python-docx.

Usage:
    python read_docx.py document.docx                    # Read full content
    python read_docx.py document.docx --structure        # Show document structure
    python read_docx.py document.docx --images output/   # Extract images
"""

import argparse
import os
import sys
from pathlib import Path

try:
    from docx import Document
    from docx.opc.constants import RELATIONSHIP_TYPE as RT
except ImportError:
    print("Error: python-docx not installed. Run: pip install python-docx")
    sys.exit(1)


def format_table_recursive(table, table_id="T1", indent=0):
    """递归格式化表格内容（包括嵌套表格）
    
    Args:
        table: docx Table 对象
        table_id: 表格标识符
        indent: 缩进级别
    
    Returns:
        list: 格式化的行列表
    """
    prefix = "  " * indent
    lines = []
    
    rows_count = len(table.rows)
    cols_count = len(table.columns)
    lines.append(f"{prefix}[{table_id}] ({rows_count}行 x {cols_count}列)")
    
    for row_idx, row in enumerate(table.rows):
        row_texts = []
        nested_tables = []
        
        for col_idx, cell in enumerate(row.cells):
            cell_text = cell.text.replace("\n", " ").strip()[:50]
            row_texts.append(cell_text if cell_text else "(空)")
            
            # 收集嵌套表格
            for nested_idx, nested_table in enumerate(cell.tables):
                nested_id = f"{table_id}.C[{row_idx},{col_idx}].T{nested_idx + 1}"
                nested_tables.append((f"[{row_idx},{col_idx}]", nested_table, nested_id))
        
        lines.append(f"{prefix}  | " + " | ".join(row_texts) + " |")
        
        # 递归处理嵌套表格
        for pos, nested_table, nested_id in nested_tables:
            lines.append(f"{prefix}  ↳ 单元格{pos}包含嵌套表格:")
            lines.extend(format_table_recursive(nested_table, nested_id, indent + 2))
    
    return lines


def read_full_content(doc):
    """Read all text content from document including nested tables."""
    content = []

    for para in doc.paragraphs:
        if para.text.strip():
            style = para.style.name
            if style.startswith("Heading"):
                level = style.replace("Heading ", "").replace("Heading", "1")
                prefix = "#" * int(level) if level.isdigit() else "#"
                content.append(f"{prefix} {para.text}")
            else:
                content.append(para.text)

    for i, table in enumerate(doc.tables):
        content.append("")
        content.extend(format_table_recursive(table, f"T{i + 1}"))

    return "\n".join(content)


def count_nested_tables(table):
    """统计表格及其嵌套表格的总数"""
    count = 1  # 当前表格
    for row in table.rows:
        for cell in row.cells:
            for nested in cell.tables:
                count += count_nested_tables(nested)
    return count


def show_table_structure(table, table_id="T1", indent=2):
    """显示表格结构（包括嵌套）"""
    lines = []
    rows = len(table.rows)
    cols = len(table.columns)
    prefix = " " * indent
    
    # 检查是否有嵌套表格
    nested_count = count_nested_tables(table) - 1
    nested_info = f" (含{nested_count}个嵌套表格)" if nested_count > 0 else ""
    lines.append(f"{prefix}{table_id}: {rows}行 x {cols}列{nested_info}")
    
    # 显示嵌套表格详情
    for row_idx, row in enumerate(table.rows):
        for col_idx, cell in enumerate(row.cells):
            for nested_idx, nested in enumerate(cell.tables):
                nested_id = f"{table_id}.C[{row_idx},{col_idx}].T{nested_idx + 1}"
                lines.extend(show_table_structure(nested, nested_id, indent + 4))
    
    return lines


def show_structure(doc):
    """Show document structure overview."""
    lines = ["Document Structure:", "=" * 40]

    # Sections
    lines.append(f"\nSections: {len(doc.sections)}")
    for i, section in enumerate(doc.sections):
        width = section.page_width.inches
        height = section.page_height.inches
        lines.append(f"  Section {i + 1}: {width:.1f}\" x {height:.1f}\"")

    # Headings outline
    lines.append("\nOutline:")
    for para in doc.paragraphs:
        if para.style.name.startswith("Heading") and para.text.strip():
            level = para.style.name.replace("Heading ", "").replace("Heading", "1")
            indent = "  " * (int(level) if level.isdigit() else 1)
            lines.append(f"{indent}- {para.text[:60]}{'...' if len(para.text) > 60 else ''}")

    # Tables (包含嵌套统计)
    total_nested = sum(count_nested_tables(t) - 1 for t in doc.tables)
    if total_nested > 0:
        lines.append(f"\nTables: {len(doc.tables)} (包含{total_nested}个嵌套表格)")
    else:
        lines.append(f"\nTables: {len(doc.tables)}")
    
    for i, table in enumerate(doc.tables):
        lines.extend(show_table_structure(table, f"T{i + 1}"))

    # Images
    image_count = sum(1 for rel in doc.part.rels.values() if "image" in rel.target_ref)
    lines.append(f"\nImages: {image_count}")

    return "\n".join(lines)


def extract_images(doc, output_dir):
    """Extract all images to output directory."""
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)

    count = 0
    for rel in doc.part.rels.values():
        if "image" in rel.target_ref:
            image_data = rel.target_part.blob
            image_name = os.path.basename(rel.target_ref)
            image_path = output_path / image_name
            image_path.write_bytes(image_data)
            print(f"Extracted: {image_path}")
            count += 1

    return f"Extracted {count} images to {output_dir}"


def main():
    parser = argparse.ArgumentParser(description="Read Word documents")
    parser.add_argument("docx_file", help="Path to .docx file")
    parser.add_argument("--structure", action="store_true", help="Show document structure")
    parser.add_argument("--images", metavar="DIR", help="Extract images to directory")
    args = parser.parse_args()

    docx_path = Path(args.docx_file)
    if not docx_path.exists():
        print(f"Error: File not found: {docx_path}")
        sys.exit(1)

    doc = Document(str(docx_path))

    if args.structure:
        print(show_structure(doc))
    elif args.images:
        print(extract_images(doc, args.images))
    else:
        print(read_full_content(doc))


if __name__ == "__main__":
    main()

