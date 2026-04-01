#!/usr/bin/env python3
"""
Fill Word document templates with data from JSON.

Usage:
    python fill_template.py template.docx data.json -o output.docx
    python fill_template.py template.docx data.json --batch -o ./output/
    python fill_template.py template.docx '{"name": "John"}' -o output.docx

Examples:
    # Single document from JSON file
    python fill_template.py contract_template.docx client_data.json -o contract.docx
    
    # Batch generation from JSON array
    python fill_template.py template.docx clients.json --batch -o ./contracts/
    
    # Inline JSON data
    python fill_template.py letter.docx '{"recipient": "Alice", "date": "2025-01-01"}' -o letter.docx

Template format:
    Use {{variable_name}} as placeholders in the Word document.
    Example: "Dear {{recipient}}, Thank you for your order on {{date}}."
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path

try:
    from docx import Document
except ImportError:
    print("Error: python-docx not installed. Run: pip install python-docx")
    sys.exit(1)


def replace_placeholders(doc, data, placeholder_pattern=r'\{\{(\w+)\}\}'):
    """
    Replace all placeholders in document with data values.
    
    Args:
        doc: Document object
        data: Dictionary with replacement values
        placeholder_pattern: Regex pattern for placeholders
    
    Returns:
        Set of replaced placeholder names
    """
    replaced = set()
    pattern = re.compile(placeholder_pattern)
    
    def replace_in_runs(paragraph):
        """Replace placeholders in paragraph runs, preserving formatting."""
        full_text = paragraph.text
        matches = list(pattern.finditer(full_text))
        
        if not matches:
            return
        
        # For each placeholder found
        for match in matches:
            key = match.group(1)
            placeholder = match.group(0)
            
            if key in data:
                value = str(data[key])
                
                # Try to replace in individual runs first
                for run in paragraph.runs:
                    if placeholder in run.text:
                        run.text = run.text.replace(placeholder, value)
                        replaced.add(key)
                        break
                else:
                    # Placeholder might span multiple runs - rebuild paragraph
                    if placeholder in paragraph.text:
                        # Find and combine runs containing the placeholder
                        combined_text = paragraph.text.replace(placeholder, value)
                        # Clear all runs and add new one (loses some formatting)
                        for run in paragraph.runs:
                            run.text = ""
                        if paragraph.runs:
                            paragraph.runs[0].text = combined_text
                        replaced.add(key)
    
    # Process paragraphs
    for para in doc.paragraphs:
        replace_in_runs(para)
    
    # Process tables
    for table in doc.tables:
        for row in table.rows:
            for cell in row.cells:
                for para in cell.paragraphs:
                    replace_in_runs(para)
    
    # Process headers and footers
    for section in doc.sections:
        for para in section.header.paragraphs:
            replace_in_runs(para)
        for para in section.footer.paragraphs:
            replace_in_runs(para)
    
    return replaced


def fill_template(template_path, data, output_path):
    """
    Fill a template document with data and save.
    
    Args:
        template_path: Path to template document
        data: Dictionary with replacement values
        output_path: Path to save filled document
    
    Returns:
        Set of replaced placeholder names
    """
    doc = Document(template_path)
    replaced = replace_placeholders(doc, data)
    doc.save(output_path)
    return replaced


def batch_fill(template_path, records, output_dir, filename_key=None):
    """
    Batch generate documents from template.
    
    Args:
        template_path: Path to template document
        records: List of data dictionaries
        output_dir: Directory to save generated documents
        filename_key: Key in data to use for filename (optional)
    
    Returns:
        List of generated file paths
    """
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    
    generated = []
    
    for i, record in enumerate(records):
        # Determine filename
        if filename_key and filename_key in record:
            # Sanitize filename
            safe_name = "".join(c if c.isalnum() or c in '-_' else '_' for c in str(record[filename_key]))
            filename = f"{safe_name}.docx"
        else:
            filename = f"document_{i+1:04d}.docx"
        
        output_file = output_path / filename
        
        # Fill template
        replaced = fill_template(template_path, record, str(output_file))
        generated.append(str(output_file))
        
        print(f"[{i+1}/{len(records)}] Generated: {filename} ({len(replaced)} replacements)")
    
    return generated


def main():
    parser = argparse.ArgumentParser(
        description="Fill Word document templates with data",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__
    )
    parser.add_argument("template", help="Path to template .docx file")
    parser.add_argument("data", help="JSON file path or inline JSON string")
    parser.add_argument("-o", "--output", required=True,
                        help="Output file path or directory (for batch)")
    parser.add_argument("--batch", action="store_true",
                        help="Batch mode: generate multiple documents from JSON array")
    parser.add_argument("--filename-key", 
                        help="Key in data to use for output filename (batch mode)")
    parser.add_argument("--list-placeholders", action="store_true",
                        help="List all placeholders in template and exit")
    
    args = parser.parse_args()
    
    # Validate template
    template_path = Path(args.template)
    if not template_path.exists():
        print(f"Error: Template not found: {template_path}")
        sys.exit(1)
    
    # List placeholders mode
    if args.list_placeholders:
        doc = Document(str(template_path))
        placeholders = set()
        pattern = re.compile(r'\{\{(\w+)\}\}')
        
        for para in doc.paragraphs:
            placeholders.update(pattern.findall(para.text))
        for table in doc.tables:
            for row in table.rows:
                for cell in row.cells:
                    for para in cell.paragraphs:
                        placeholders.update(pattern.findall(para.text))
        
        if placeholders:
            print("Placeholders found in template:")
            for p in sorted(placeholders):
                print(f"  {{{{ {p} }}}}")
        else:
            print("No placeholders found in template.")
        return
    
    # Parse data
    try:
        # Try as file path first
        data_path = Path(args.data)
        if data_path.exists():
            with open(data_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
        else:
            # Try as inline JSON
            data = json.loads(args.data)
    except json.JSONDecodeError as e:
        print(f"Error: Invalid JSON - {e}")
        sys.exit(1)
    except Exception as e:
        print(f"Error: Failed to load data - {e}")
        sys.exit(1)
    
    # Process
    if args.batch:
        if not isinstance(data, list):
            print("Error: Batch mode requires JSON array")
            sys.exit(1)
        
        generated = batch_fill(
            str(template_path), 
            data, 
            args.output,
            args.filename_key
        )
        print(f"\nBatch complete: {len(generated)} documents generated")
    else:
        if isinstance(data, list):
            print("Warning: Data is an array, using first element")
            data = data[0] if data else {}
        
        replaced = fill_template(str(template_path), data, args.output)
        print(f"Generated: {args.output}")
        print(f"Replaced {len(replaced)} placeholders: {', '.join(sorted(replaced))}")


if __name__ == "__main__":
    main()

