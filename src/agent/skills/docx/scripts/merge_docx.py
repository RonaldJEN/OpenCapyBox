#!/usr/bin/env python3
"""
Merge multiple Word documents into one.

Usage:
    python merge_docx.py doc1.docx doc2.docx doc3.docx -o merged.docx
    python merge_docx.py *.docx -o all_merged.docx
    python merge_docx.py --dir ./documents -o merged.docx
    python merge_docx.py doc1.docx doc2.docx -o merged.docx --no-page-break

Examples:
    # Merge specific files
    python merge_docx.py chapter1.docx chapter2.docx chapter3.docx -o book.docx
    
    # Merge all docx in directory
    python merge_docx.py --dir ./chapters -o complete_book.docx
    
    # Merge without page breaks between documents
    python merge_docx.py part1.docx part2.docx -o combined.docx --no-page-break
    
    # Merge and sort by filename
    python merge_docx.py --dir ./docs -o merged.docx --sort
"""

import argparse
import glob
import sys
from pathlib import Path

try:
    from docx import Document
except ImportError:
    print("Error: python-docx not installed. Run: pip install python-docx")
    sys.exit(1)

# Try to import docxcompose for better merging
try:
    from docxcompose.composer import Composer
    HAS_DOCXCOMPOSE = True
except ImportError:
    HAS_DOCXCOMPOSE = False


def merge_with_docxcompose(doc_paths, output_path, add_page_breaks=True):
    """
    Merge documents using docxcompose (preserves formatting better).
    
    Args:
        doc_paths: List of document paths
        output_path: Output file path
        add_page_breaks: Whether to add page breaks between documents
    """
    if not doc_paths:
        raise ValueError("No documents to merge")
    
    # First document as master
    master = Document(doc_paths[0])
    composer = Composer(master)
    
    # Append remaining documents
    for i, doc_path in enumerate(doc_paths[1:], 1):
        doc = Document(doc_path)
        
        if add_page_breaks:
            # Add page break before appending
            master.add_page_break()
        
        composer.append(doc)
        print(f"  [{i+1}/{len(doc_paths)}] Added: {Path(doc_path).name}")
    
    composer.save(output_path)


def merge_simple(doc_paths, output_path, add_page_breaks=True):
    """
    Simple merge using python-docx only.
    
    Args:
        doc_paths: List of document paths
        output_path: Output file path
        add_page_breaks: Whether to add page breaks between documents
    """
    if not doc_paths:
        raise ValueError("No documents to merge")
    
    result = Document()
    
    for i, doc_path in enumerate(doc_paths):
        doc = Document(doc_path)
        
        # Add page break (except for first document)
        if i > 0 and add_page_breaks:
            result.add_page_break()
        
        # Copy paragraphs
        for para in doc.paragraphs:
            new_para = result.add_paragraph()
            new_para.style = para.style
            new_para.paragraph_format.alignment = para.paragraph_format.alignment
            
            for run in para.runs:
                new_run = new_para.add_run(run.text)
                new_run.bold = run.bold
                new_run.italic = run.italic
                new_run.underline = run.underline
                if run.font.size:
                    new_run.font.size = run.font.size
                if run.font.name:
                    new_run.font.name = run.font.name
                if run.font.color.rgb:
                    new_run.font.color.rgb = run.font.color.rgb
        
        # Copy tables
        for table in doc.tables:
            new_table = result.add_table(rows=0, cols=len(table.columns))
            new_table.style = table.style
            
            for row in table.rows:
                new_row = new_table.add_row()
                for i, cell in enumerate(row.cells):
                    new_row.cells[i].text = cell.text
        
        print(f"  [{i+1}/{len(doc_paths)}] Added: {Path(doc_path).name}")
    
    result.save(output_path)


def merge_documents(doc_paths, output_path, add_page_breaks=True, use_simple=False):
    """
    Merge multiple documents into one.
    
    Args:
        doc_paths: List of document paths
        output_path: Output file path
        add_page_breaks: Whether to add page breaks between documents
        use_simple: Force simple merge even if docxcompose is available
    """
    if HAS_DOCXCOMPOSE and not use_simple:
        print("Using docxcompose for merging (better formatting preservation)")
        merge_with_docxcompose(doc_paths, output_path, add_page_breaks)
    else:
        if not use_simple:
            print("docxcompose not installed, using simple merge")
            print("Install with: pip install docxcompose")
        merge_simple(doc_paths, output_path, add_page_breaks)


def get_documents_from_dir(directory, pattern="*.docx", sort=False):
    """
    Get all docx files from directory.
    
    Args:
        directory: Directory path
        pattern: Glob pattern
        sort: Whether to sort by filename
    
    Returns:
        List of document paths
    """
    dir_path = Path(directory)
    if not dir_path.exists():
        raise ValueError(f"Directory not found: {directory}")
    
    files = list(dir_path.glob(pattern))
    
    if sort:
        files.sort(key=lambda x: x.name.lower())
    
    return [str(f) for f in files]


def main():
    parser = argparse.ArgumentParser(
        description="Merge multiple Word documents into one",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__
    )
    parser.add_argument("files", nargs="*", help="Document files to merge")
    parser.add_argument("-o", "--output", required=True, help="Output file path")
    parser.add_argument("--dir", help="Directory containing documents to merge")
    parser.add_argument("--pattern", default="*.docx", 
                        help="File pattern when using --dir (default: *.docx)")
    parser.add_argument("--no-page-break", action="store_true",
                        help="Don't add page breaks between documents")
    parser.add_argument("--sort", action="store_true",
                        help="Sort files by name before merging")
    parser.add_argument("--simple", action="store_true",
                        help="Use simple merge method (python-docx only)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would be merged without actually merging")
    
    args = parser.parse_args()
    
    # Collect document paths
    doc_paths = []
    
    if args.dir:
        doc_paths = get_documents_from_dir(args.dir, args.pattern, args.sort)
    elif args.files:
        # Expand globs
        for pattern in args.files:
            expanded = glob.glob(pattern)
            if expanded:
                doc_paths.extend(expanded)
            else:
                doc_paths.append(pattern)
        
        if args.sort:
            doc_paths.sort(key=lambda x: Path(x).name.lower())
    else:
        parser.error("Either provide document files or use --dir")
    
    # Validate documents exist
    valid_paths = []
    for path in doc_paths:
        p = Path(path)
        if p.exists() and p.suffix.lower() == '.docx':
            valid_paths.append(str(p))
        else:
            print(f"Warning: Skipping invalid file: {path}")
    
    if not valid_paths:
        print("Error: No valid documents to merge")
        sys.exit(1)
    
    # Dry run
    if args.dry_run:
        print(f"Would merge {len(valid_paths)} documents into: {args.output}")
        for i, p in enumerate(valid_paths, 1):
            print(f"  {i}. {Path(p).name}")
        return
    
    # Merge
    print(f"Merging {len(valid_paths)} documents...")
    try:
        merge_documents(
            valid_paths, 
            args.output,
            add_page_breaks=not args.no_page_break,
            use_simple=args.simple
        )
        print(f"\nMerge complete: {args.output}")
    except Exception as e:
        print(f"Error during merge: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()

