#!/usr/bin/env python3
"""
Edit Word documents: text replacement, find & replace with regex support.

Usage:
    # Simple replacement
    python edit_docx.py document.docx --replace "old text" "new text" -o output.docx
    
    # Multiple replacements
    python edit_docx.py document.docx --replace "借款人" "被投资人xxx" --replace "保证人" "收购人yyy" -o output.docx
    
    # Batch replacements from JSON
    python edit_docx.py document.docx --replacements replacements.json -o output.docx
    
    # Regex replacement
    python edit_docx.py document.docx --regex "\\d{4}-\\d{2}-\\d{2}" "2025-01-01" -o output.docx
    
    # Preview changes (dry run)
    python edit_docx.py document.docx --replace "old" "new" --dry-run

Examples:
    # Replace company names
    python edit_docx.py template.docx --replace "莆田市涵江区兴化湾港口开发有限公司" "广东文科绿色科技股份有限公司" -o filled.docx
    
    # Batch from JSON file (replacements.json format: {"借款人": "被投资人xxx", "保证人": "收购人yyy"})
    python edit_docx.py template.docx --replacements data.json -o filled.docx
"""

import argparse
import json
import re
import sys
from pathlib import Path
from copy import deepcopy

try:
    from docx import Document
except ImportError:
    print("Error: python-docx not installed. Run: pip install python-docx")
    sys.exit(1)


def replace_in_paragraph(paragraph, replacements, use_regex=False):
    """
    Replace text in paragraph while preserving formatting.
    
    Args:
        paragraph: docx Paragraph object
        replacements: dict of {old_text: new_text}
        use_regex: if True, treat keys as regex patterns
    
    Returns:
        Number of replacements made
    """
    count = 0
    
    for old_text, new_text in replacements.items():
        # Check if paragraph contains the text
        full_text = paragraph.text
        
        if use_regex:
            if not re.search(old_text, full_text):
                continue
        else:
            if old_text not in full_text:
                continue
        
        # Try to replace in individual runs first (preserves formatting)
        replaced_in_run = False
        for run in paragraph.runs:
            if use_regex:
                if re.search(old_text, run.text):
                    run.text = re.sub(old_text, new_text, run.text)
                    replaced_in_run = True
                    count += 1
            else:
                if old_text in run.text:
                    run.text = run.text.replace(old_text, new_text)
                    replaced_in_run = True
                    count += 1
        
        # If text spans multiple runs, combine and replace (loses some formatting)
        if not replaced_in_run and paragraph.runs:
            if use_regex:
                new_full_text = re.sub(old_text, new_text, full_text)
            else:
                new_full_text = full_text.replace(old_text, new_text)
            
            if new_full_text != full_text:
                # Clear all runs except first
                for run in paragraph.runs[1:]:
                    run.text = ""
                paragraph.runs[0].text = new_full_text
                count += 1
    
    return count


def replace_in_table_recursive(table, replacements, use_regex=False):
    """
    Recursively replace text in table including nested tables.
    
    Args:
        table: docx Table object
        replacements: dict of {old_text: new_text}
        use_regex: if True, treat keys as regex patterns
    
    Returns:
        Number of replacements made
    """
    count = 0
    
    for row in table.rows:
        for cell in row.cells:
            # Replace in cell paragraphs
            for para in cell.paragraphs:
                count += replace_in_paragraph(para, replacements, use_regex)
            
            # Recursively handle nested tables
            for nested_table in cell.tables:
                count += replace_in_table_recursive(nested_table, replacements, use_regex)
    
    return count


def edit_document(doc_path, replacements, output_path=None, use_regex=False, dry_run=False):
    """
    Edit document by replacing text.
    
    Args:
        doc_path: Path to source document
        replacements: dict of {old_text: new_text}
        output_path: Path to save modified document (optional, overwrites if None)
        use_regex: if True, treat keys as regex patterns
        dry_run: if True, only preview changes without saving
    
    Returns:
        dict with replacement statistics
    """
    doc = Document(doc_path)
    stats = {
        "paragraphs": 0,
        "tables": 0,
        "headers_footers": 0,
        "total": 0,
        "details": []
    }
    
    # Replace in main paragraphs
    for para in doc.paragraphs:
        count = replace_in_paragraph(para, replacements, use_regex)
        if count > 0:
            stats["paragraphs"] += count
            stats["total"] += count
    
    # Replace in tables (including nested)
    for table in doc.tables:
        count = replace_in_table_recursive(table, replacements, use_regex)
        if count > 0:
            stats["tables"] += count
            stats["total"] += count
    
    # Replace in headers and footers
    for section in doc.sections:
        for para in section.header.paragraphs:
            count = replace_in_paragraph(para, replacements, use_regex)
            if count > 0:
                stats["headers_footers"] += count
                stats["total"] += count
        
        for para in section.footer.paragraphs:
            count = replace_in_paragraph(para, replacements, use_regex)
            if count > 0:
                stats["headers_footers"] += count
                stats["total"] += count
        
        # Also check tables in headers/footers
        for table in section.header.tables:
            count = replace_in_table_recursive(table, replacements, use_regex)
            if count > 0:
                stats["headers_footers"] += count
                stats["total"] += count
        
        for table in section.footer.tables:
            count = replace_in_table_recursive(table, replacements, use_regex)
            if count > 0:
                stats["headers_footers"] += count
                stats["total"] += count
    
    # Save if not dry run
    if not dry_run:
        save_path = output_path or doc_path
        doc.save(save_path)
        stats["saved_to"] = str(save_path)
    
    return stats


def main():
    parser = argparse.ArgumentParser(
        description="Edit Word documents with text replacement",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__
    )
    parser.add_argument("docx_file", help="Path to .docx file to edit")
    parser.add_argument("-o", "--output", help="Output file path (default: overwrite input)")
    parser.add_argument("--replace", nargs=2, action="append", metavar=("OLD", "NEW"),
                        help="Replace OLD with NEW (can be used multiple times)")
    parser.add_argument("--replacements", metavar="JSON",
                        help="JSON file or string with replacements dict")
    parser.add_argument("--regex", nargs=2, action="append", metavar=("PATTERN", "REPLACEMENT"),
                        help="Regex replacement (can be used multiple times)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Preview changes without saving")
    
    args = parser.parse_args()
    
    # Validate input
    doc_path = Path(args.docx_file)
    if not doc_path.exists():
        print(f"Error: File not found: {doc_path}")
        sys.exit(1)
    
    # Collect replacements
    replacements = {}
    use_regex = False
    
    if args.replace:
        for old, new in args.replace:
            replacements[old] = new
    
    if args.regex:
        use_regex = True
        for pattern, replacement in args.regex:
            replacements[pattern] = replacement
    
    if args.replacements:
        try:
            # Try as file path first
            repl_path = Path(args.replacements)
            if repl_path.exists():
                with open(repl_path, 'r', encoding='utf-8') as f:
                    json_replacements = json.load(f)
            else:
                # Try as inline JSON
                json_replacements = json.loads(args.replacements)
            
            if isinstance(json_replacements, dict):
                replacements.update(json_replacements)
            else:
                print("Error: Replacements JSON must be an object/dict")
                sys.exit(1)
        except json.JSONDecodeError as e:
            print(f"Error: Invalid JSON - {e}")
            sys.exit(1)
    
    if not replacements:
        print("Error: No replacements specified. Use --replace, --regex, or --replacements")
        sys.exit(1)
    
    # Show what will be replaced
    print(f"{'[DRY RUN] ' if args.dry_run else ''}Editing: {doc_path}")
    print(f"Replacements ({len(replacements)}):")
    for old, new in replacements.items():
        old_preview = old[:40] + "..." if len(old) > 40 else old
        new_preview = new[:40] + "..." if len(new) > 40 else new
        print(f"  '{old_preview}' → '{new_preview}'")
    print()
    
    # Perform replacements
    stats = edit_document(
        str(doc_path),
        replacements,
        args.output,
        use_regex=use_regex,
        dry_run=args.dry_run
    )
    
    # Report results
    print("Results:")
    print(f"  Paragraphs: {stats['paragraphs']} replacements")
    print(f"  Tables: {stats['tables']} replacements")
    print(f"  Headers/Footers: {stats['headers_footers']} replacements")
    print(f"  Total: {stats['total']} replacements")
    
    if not args.dry_run:
        print(f"\nSaved to: {stats.get('saved_to', args.output or doc_path)}")
    else:
        print("\n[DRY RUN] No changes saved.")


if __name__ == "__main__":
    main()

