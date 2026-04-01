#!/usr/bin/env python3
"""
Update table data in Word documents with precise cell targeting.

Usage:
    # Update specific cells by coordinates
    python update_table.py document.docx "T1" --cell 0,0 "新值" --cell 1,2 "另一个值" -o output.docx
    
    # Update nested table
    python update_table.py document.docx "T1.C[4,1].T1" --cell 0,0 "标题" -o output.docx
    
    # Batch update from JSON
    python update_table.py document.docx "T1" --data table_data.json -o output.docx
    
    # Update entire row
    python update_table.py document.docx "T1" --row 2 '["A", "B", "C", "D"]' -o output.docx
    
    # List all tables (for discovery)
    python update_table.py document.docx --list

Table Path Format:
    T1                    - First top-level table
    T2                    - Second top-level table
    T1.C[4,1].T1          - First nested table in cell [row=4, col=1] of T1
    T1.C[4,1].T1.C[0,0].T1 - Deeper nesting

JSON Data Format:
    {
        "cells": {
            "0,0": "Value at row 0, col 0",
            "1,2": "Value at row 1, col 2"
        },
        "rows": {
            "0": ["Col1", "Col2", "Col3"],
            "2": ["A", "B", "C"]
        }
    }

Examples:
    # Update financial data table
    python update_table.py report.docx "T1.C[4,1].T1" --data financial_data.json -o report_updated.docx
    
    # Quick cell updates
    python update_table.py doc.docx "T1" --cell 0,0 "公司名称" --cell 0,1 "广东文科绿色科技股份有限公司" -o out.docx
"""

import argparse
import json
import re
import sys
from pathlib import Path

try:
    from docx import Document
    from docx.table import Table
except ImportError:
    print("Error: python-docx not installed. Run: pip install python-docx")
    sys.exit(1)


def parse_table_path(path_str):
    """
    Parse table path string into navigation instructions.
    
    Args:
        path_str: e.g., "T1", "T1.C[4,1].T1", "T2.C[0,0].T1.C[1,2].T2"
    
    Returns:
        list of navigation steps: [("table", 0), ("cell", 4, 1), ("table", 0), ...]
    """
    steps = []
    parts = path_str.split(".")
    
    for part in parts:
        # Match table index: T1, T2, etc.
        table_match = re.match(r'^T(\d+)$', part)
        if table_match:
            steps.append(("table", int(table_match.group(1)) - 1))  # Convert to 0-based
            continue
        
        # Match cell reference: C[row,col]
        cell_match = re.match(r'^C\[(\d+),(\d+)\]$', part)
        if cell_match:
            row = int(cell_match.group(1))
            col = int(cell_match.group(2))
            steps.append(("cell", row, col))
            continue
        
        raise ValueError(f"Invalid path component: {part}")
    
    return steps


def navigate_to_table(doc, table_path):
    """
    Navigate to a specific table (including nested tables) by path.
    
    Args:
        doc: Document object
        table_path: Table path string (e.g., "T1.C[4,1].T1")
    
    Returns:
        Table object or None if not found
    """
    steps = parse_table_path(table_path)
    
    current = doc
    current_type = "document"
    
    for step in steps:
        if step[0] == "table":
            table_idx = step[1]
            
            if current_type == "document":
                tables = current.tables
            elif current_type == "cell":
                tables = current.tables
            else:
                raise ValueError(f"Cannot get tables from {current_type}")
            
            if table_idx >= len(tables):
                raise ValueError(f"Table index {table_idx + 1} out of range (found {len(tables)} tables)")
            
            current = tables[table_idx]
            current_type = "table"
        
        elif step[0] == "cell":
            row_idx, col_idx = step[1], step[2]
            
            if current_type != "table":
                raise ValueError(f"Cannot get cell from {current_type}")
            
            if row_idx >= len(current.rows):
                raise ValueError(f"Row index {row_idx} out of range (table has {len(current.rows)} rows)")
            
            row = current.rows[row_idx]
            if col_idx >= len(row.cells):
                raise ValueError(f"Column index {col_idx} out of range (row has {len(row.cells)} columns)")
            
            current = row.cells[col_idx]
            current_type = "cell"
    
    if current_type != "table":
        raise ValueError(f"Path does not end at a table (ends at {current_type})")
    
    return current


def set_cell_text(cell, text, preserve_format=True):
    """
    Set cell text, optionally preserving formatting.
    
    Args:
        cell: Table cell object
        text: New text content
        preserve_format: If True, try to preserve existing formatting
    """
    if preserve_format and cell.paragraphs and cell.paragraphs[0].runs:
        # Clear existing text but keep first run's formatting
        first_para = cell.paragraphs[0]
        for para in cell.paragraphs[1:]:
            para.clear()
        for run in first_para.runs[1:]:
            run.text = ""
        first_para.runs[0].text = str(text)
    else:
        # Simple replacement
        cell.text = str(text)


def update_table_cells(table, cell_updates):
    """
    Update specific cells in a table.
    
    Args:
        table: Table object
        cell_updates: dict of {"row,col": value} or {(row, col): value}
    
    Returns:
        Number of cells updated
    """
    count = 0
    
    for key, value in cell_updates.items():
        # Parse key
        if isinstance(key, tuple):
            row_idx, col_idx = key
        elif isinstance(key, str):
            parts = key.split(",")
            if len(parts) != 2:
                print(f"Warning: Invalid cell key '{key}', skipping")
                continue
            row_idx, col_idx = int(parts[0].strip()), int(parts[1].strip())
        else:
            print(f"Warning: Invalid cell key type {type(key)}, skipping")
            continue
        
        # Validate indices
        if row_idx >= len(table.rows):
            print(f"Warning: Row {row_idx} out of range, skipping")
            continue
        
        row = table.rows[row_idx]
        if col_idx >= len(row.cells):
            print(f"Warning: Column {col_idx} out of range for row {row_idx}, skipping")
            continue
        
        # Update cell
        cell = row.cells[col_idx]
        set_cell_text(cell, value)
        count += 1
    
    return count


def update_table_rows(table, row_updates):
    """
    Update entire rows in a table.
    
    Args:
        table: Table object
        row_updates: dict of {row_index: [col0_val, col1_val, ...]}
    
    Returns:
        Number of cells updated
    """
    count = 0
    
    for row_key, values in row_updates.items():
        row_idx = int(row_key) if isinstance(row_key, str) else row_key
        
        if row_idx >= len(table.rows):
            print(f"Warning: Row {row_idx} out of range, skipping")
            continue
        
        row = table.rows[row_idx]
        
        for col_idx, value in enumerate(values):
            if col_idx >= len(row.cells):
                print(f"Warning: Column {col_idx} out of range for row {row_idx}, skipping")
                continue
            
            if value is not None:  # Allow None to skip cells
                cell = row.cells[col_idx]
                set_cell_text(cell, value)
                count += 1
    
    return count


def list_tables_recursive(container, prefix="", level=0):
    """
    Recursively list all tables with their paths.
    
    Args:
        container: Document or Cell object
        prefix: Current path prefix
        level: Nesting level
    
    Returns:
        list of (path, rows, cols, preview) tuples
    """
    tables = []
    
    container_tables = container.tables if hasattr(container, 'tables') else []
    
    for i, table in enumerate(container_tables):
        table_id = f"T{i + 1}"
        full_path = f"{prefix}{table_id}" if not prefix else f"{prefix}.{table_id}"
        
        rows = len(table.rows)
        cols = len(table.columns) if table.rows else 0
        
        # Get preview of first cell
        preview = ""
        if table.rows and table.rows[0].cells:
            preview = table.rows[0].cells[0].text[:30].replace("\n", " ")
            if len(table.rows[0].cells[0].text) > 30:
                preview += "..."
        
        tables.append((full_path, rows, cols, preview))
        
        # Check for nested tables
        for row_idx, row in enumerate(table.rows):
            for col_idx, cell in enumerate(row.cells):
                if cell.tables:
                    nested_prefix = f"{full_path}.C[{row_idx},{col_idx}]"
                    tables.extend(list_tables_recursive(cell, nested_prefix, level + 1))
    
    return tables


def main():
    parser = argparse.ArgumentParser(
        description="Update table data in Word documents",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__
    )
    parser.add_argument("docx_file", help="Path to .docx file")
    parser.add_argument("table_path", nargs="?", help="Table path (e.g., T1, T1.C[4,1].T1)")
    parser.add_argument("-o", "--output", help="Output file path")
    parser.add_argument("--list", action="store_true", help="List all tables and exit")
    parser.add_argument("--cell", nargs=2, action="append", metavar=("ROW,COL", "VALUE"),
                        help="Update cell at ROW,COL with VALUE")
    parser.add_argument("--row", nargs=2, action="append", metavar=("INDEX", "JSON_ARRAY"),
                        help="Update entire row with JSON array of values")
    parser.add_argument("--data", metavar="JSON",
                        help="JSON file or string with update data")
    parser.add_argument("--dry-run", action="store_true",
                        help="Preview target table without saving")
    
    args = parser.parse_args()
    
    # Validate input
    doc_path = Path(args.docx_file)
    if not doc_path.exists():
        print(f"Error: File not found: {doc_path}")
        sys.exit(1)
    
    doc = Document(str(doc_path))
    
    # List mode
    if args.list:
        print(f"Tables in {doc_path}:\n")
        tables = list_tables_recursive(doc)
        
        if not tables:
            print("  No tables found.")
        else:
            for path, rows, cols, preview in tables:
                indent = "  " * path.count(".")
                print(f"{indent}{path}: {rows} rows × {cols} cols")
                if preview:
                    print(f"{indent}  Preview: {preview}")
        return
    
    # Require table_path for updates
    if not args.table_path:
        print("Error: table_path is required for updates. Use --list to see available tables.")
        sys.exit(1)
    
    # Navigate to target table
    try:
        table = navigate_to_table(doc, args.table_path)
        print(f"Target table: {args.table_path} ({len(table.rows)} rows × {len(table.columns)} cols)")
    except ValueError as e:
        print(f"Error: {e}")
        sys.exit(1)
    
    # Dry run - just show table info
    if args.dry_run:
        print("\n[DRY RUN] Table content preview:")
        for i, row in enumerate(table.rows[:5]):  # Show first 5 rows
            cells = [cell.text[:20].replace("\n", " ") for cell in row.cells]
            print(f"  Row {i}: {cells}")
        if len(table.rows) > 5:
            print(f"  ... and {len(table.rows) - 5} more rows")
        return
    
    # Collect updates
    cell_updates = {}
    row_updates = {}
    
    if args.cell:
        for coords, value in args.cell:
            cell_updates[coords] = value
    
    if args.row:
        for index, json_array in args.row:
            try:
                values = json.loads(json_array)
                if not isinstance(values, list):
                    print(f"Error: Row value must be a JSON array, got {type(values)}")
                    sys.exit(1)
                row_updates[index] = values
            except json.JSONDecodeError as e:
                print(f"Error: Invalid JSON for row {index}: {e}")
                sys.exit(1)
    
    if args.data:
        try:
            # Try as file path first
            data_path = Path(args.data)
            if data_path.exists():
                with open(data_path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
            else:
                # Try as inline JSON
                data = json.loads(args.data)
            
            if "cells" in data:
                cell_updates.update(data["cells"])
            if "rows" in data:
                row_updates.update(data["rows"])
            
        except json.JSONDecodeError as e:
            print(f"Error: Invalid JSON - {e}")
            sys.exit(1)
    
    if not cell_updates and not row_updates:
        print("Error: No updates specified. Use --cell, --row, or --data")
        sys.exit(1)
    
    # Apply updates
    total_updates = 0
    
    if cell_updates:
        count = update_table_cells(table, cell_updates)
        print(f"Updated {count} cells")
        total_updates += count
    
    if row_updates:
        count = update_table_rows(table, row_updates)
        print(f"Updated {count} cells via row updates")
        total_updates += count
    
    # Save
    output_path = args.output or str(doc_path)
    doc.save(output_path)
    print(f"\nTotal: {total_updates} cells updated")
    print(f"Saved to: {output_path}")


if __name__ == "__main__":
    main()

