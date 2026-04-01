#!/usr/bin/env python3
"""
Simple PDF document reader using pypdf and pdfplumber.

Usage:
    python read_pdf.py document.pdf                    # Read full text content
    python read_pdf.py document.pdf --structure        # Show document structure
    python read_pdf.py document.pdf --tables           # Extract tables as CSV
    python read_pdf.py document.pdf --metadata         # Show PDF metadata
"""

import argparse
import csv
import os
import sys
from pathlib import Path

try:
    import pypdf
    import pdfplumber
except ImportError:
    print("Error: Required packages not installed.")
    print("Run: pip install pypdf pdfplumber")
    sys.exit(1)


def read_text_content(pdf_path):
    """Read all text content from PDF using pypdf."""
    try:
        reader = pypdf.PdfReader(pdf_path)
        text_content = []

        for page_num, page in enumerate(reader.pages, 1):
            text = page.extract_text()
            if text.strip():
                text_content.append(f"=== Page {page_num} ===\n{text.strip()}")

        return "\n\n".join(text_content) if text_content else "No text content found in PDF."
    except Exception as e:
        return f"Error reading PDF with pypdf: {e}"


def show_structure(pdf_path):
    """Show PDF document structure and metadata."""
    try:
        reader = pypdf.PdfReader(pdf_path)
        lines = ["PDF Document Structure:", "=" * 40]

        # Basic info
        lines.append(f"File: {os.path.basename(pdf_path)}")
        lines.append(f"Pages: {len(reader.pages)}")

        # Metadata
        if reader.metadata:
            meta = reader.metadata
            lines.append("\nMetadata:")
            if meta.title:
                lines.append(f"  Title: {meta.title}")
            if meta.author:
                lines.append(f"  Author: {meta.author}")
            if meta.subject:
                lines.append(f"  Subject: {meta.subject}")
            if meta.creator:
                lines.append(f"  Creator: {meta.creator}")
            if meta.producer:
                lines.append(f"  Producer: {meta.producer}")

        # Page details
        lines.append("\nPage Details:")
        for i, page in enumerate(reader.pages):
            try:
                text_length = len(page.extract_text().strip())
                lines.append(f"  Page {i+1}: {text_length} characters")
            except:
                lines.append(f"  Page {i+1}: (error reading)")

        return "\n".join(lines)
    except Exception as e:
        return f"Error analyzing PDF structure: {e}"


def extract_tables(pdf_path, output_dir=None):
    """Extract tables from PDF using pdfplumber."""
    try:
        results = []

        with pdfplumber.open(pdf_path) as pdf:
            for page_num, page in enumerate(pdf.pages, 1):
                tables = page.extract_tables()

                if tables:
                    results.append(f"=== Page {page_num}: {len(tables)} table(s) ===")

                    for table_idx, table in enumerate(tables):
                        if table and len(table) > 0:
                            # Clean table data
                            clean_table = []
                            for row in table:
                                clean_row = []
                                for cell in row:
                                    if cell is None:
                                        clean_row.append("")
                                    else:
                                        # Convert to string and clean
                                        cell_str = str(cell).replace('\n', ' ').strip()
                                        clean_row.append(cell_str)
                                clean_table.append(clean_row)

                            # Save as CSV if output_dir specified
                            if output_dir:
                                output_path = Path(output_dir)
                                output_path.mkdir(parents=True, exist_ok=True)

                                csv_filename = f"page_{page_num}_table_{table_idx + 1}.csv"
                                csv_path = output_path / csv_filename

                                with open(csv_path, 'w', newline='', encoding='utf-8') as csvfile:
                                    writer = csv.writer(csvfile)
                                    writer.writerows(clean_table)

                                results.append(f"Saved table {table_idx + 1} to {csv_filename}")

                            # Also display table content
                            results.append(f"Table {table_idx + 1} ({len(clean_table)} rows x {len(clean_table[0]) if clean_table else 0} cols):")
                            for row in clean_table[:5]:  # Show first 5 rows
                                results.append("  | " + " | ".join(row) + " |")
                            if len(clean_table) > 5:
                                results.append(f"  ... ({len(clean_table) - 5} more rows)")

        if not results:
            return "No tables found in PDF."

        return "\n".join(results)
    except Exception as e:
        return f"Error extracting tables: {e}"


def show_metadata(pdf_path):
    """Show detailed PDF metadata."""
    try:
        reader = pypdf.PdfReader(pdf_path)
        lines = ["PDF Metadata:", "=" * 20]

        if reader.metadata:
            meta = reader.metadata
            lines.append(f"Title: {meta.title or 'N/A'}")
            lines.append(f"Author: {meta.author or 'N/A'}")
            lines.append(f"Subject: {meta.subject or 'N/A'}")
            lines.append(f"Creator: {meta.creator or 'N/A'}")
            lines.append(f"Producer: {meta.producer or 'N/A'}")
            lines.append(f"Creation Date: {getattr(meta, 'creation_date', 'N/A')}")
            lines.append(f"Modification Date: {getattr(meta, 'modification_date', 'N/A')}")
        else:
            lines.append("No metadata found.")

        # Additional info
        lines.append(f"\nPages: {len(reader.pages)}")
        lines.append(f"Encrypted: {reader.is_encrypted}")

        return "\n".join(lines)
    except Exception as e:
        return f"Error reading metadata: {e}"


def main():
    parser = argparse.ArgumentParser(description="Read PDF documents")
    parser.add_argument("pdf_file", help="Path to .pdf file")
    parser.add_argument("--structure", action="store_true", help="Show document structure")
    parser.add_argument("--tables", metavar="DIR", nargs='?', const='.', help="Extract tables (optionally specify output directory)")
    parser.add_argument("--metadata", action="store_true", help="Show PDF metadata")
    args = parser.parse_args()

    pdf_path = Path(args.pdf_file)
    if not pdf_path.exists():
        print(f"Error: File not found: {pdf_path}")
        sys.exit(1)

    if args.structure:
        print(show_structure(pdf_path))
    elif args.tables is not None:
        print(extract_tables(pdf_path, args.tables))
    elif args.metadata:
        print(show_metadata(pdf_path))
    else:
        print(read_text_content(pdf_path))


if __name__ == "__main__":
    main()
