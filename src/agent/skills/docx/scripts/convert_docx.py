#!/usr/bin/env python3
"""
Convert Word documents to other formats (PDF, HTML, TXT, images).

Usage:
    python convert_docx.py document.docx --to pdf
    python convert_docx.py document.docx --to pdf -o output.pdf
    python convert_docx.py document.docx --to txt -o output.txt
    python convert_docx.py --dir ./documents --to pdf -o ./pdfs

Requirements:
    - PDF conversion: LibreOffice (soffice command)
    - Image conversion: LibreOffice + poppler-utils (pdftoppm)
    - TXT/HTML: python-docx only

Examples:
    # Convert single file to PDF
    python convert_docx.py report.docx --to pdf
    
    # Convert to PDF with specific output path
    python convert_docx.py report.docx --to pdf -o ./output/report.pdf
    
    # Extract text content
    python convert_docx.py document.docx --to txt -o content.txt
    
    # Batch convert directory to PDF
    python convert_docx.py --dir ./documents --to pdf -o ./pdfs
    
    # Convert to images (PNG per page)
    python convert_docx.py document.docx --to png -o ./images
"""

import argparse
import os
import platform
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

try:
    from docx import Document
except ImportError:
    print("Error: python-docx not installed. Run: pip install python-docx")
    sys.exit(1)


def find_libreoffice():
    """Find LibreOffice executable path."""
    system = platform.system()
    
    if system == 'Windows':
        paths = [
            r"C:\Program Files\LibreOffice\program\soffice.exe",
            r"C:\Program Files (x86)\LibreOffice\program\soffice.exe",
        ]
        for path in paths:
            if os.path.exists(path):
                return path
        # Try from PATH
        return shutil.which('soffice') or shutil.which('soffice.exe')
    
    elif system == 'Darwin':  # macOS
        paths = [
            "/Applications/LibreOffice.app/Contents/MacOS/soffice",
            "/usr/local/bin/soffice",
        ]
        for path in paths:
            if os.path.exists(path):
                return path
        return shutil.which('soffice')
    
    else:  # Linux
        return shutil.which('soffice') or shutil.which('libreoffice')


def convert_to_pdf(docx_path, output_path=None):
    """
    Convert DOCX to PDF using LibreOffice.
    
    Args:
        docx_path: Path to input DOCX file
        output_path: Optional output path (default: same name with .pdf)
    
    Returns:
        Path to generated PDF
    """
    soffice = find_libreoffice()
    if not soffice:
        raise RuntimeError(
            "LibreOffice not found. Please install LibreOffice.\n"
            "  Windows: https://www.libreoffice.org/download/\n"
            "  macOS: brew install --cask libreoffice\n"
            "  Linux: apt install libreoffice"
        )
    
    docx_path = Path(docx_path)
    
    if output_path:
        output_path = Path(output_path)
        output_dir = output_path.parent
        output_dir.mkdir(parents=True, exist_ok=True)
    else:
        output_dir = docx_path.parent
        output_path = output_dir / (docx_path.stem + '.pdf')
    
    # LibreOffice outputs to the outdir with original basename
    cmd = [
        soffice,
        '--headless',
        '--convert-to', 'pdf',
        '--outdir', str(output_dir),
        str(docx_path)
    ]
    
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"LibreOffice conversion failed: {e.stderr}")
    
    # Rename if needed
    generated_pdf = output_dir / (docx_path.stem + '.pdf')
    if output_path != generated_pdf and generated_pdf.exists():
        shutil.move(str(generated_pdf), str(output_path))
    
    return output_path


def convert_to_txt(docx_path, output_path=None):
    """
    Extract text from DOCX to plain text file.
    
    Args:
        docx_path: Path to input DOCX file
        output_path: Optional output path (default: same name with .txt)
    
    Returns:
        Path to generated TXT file
    """
    docx_path = Path(docx_path)
    
    if output_path:
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
    else:
        output_path = docx_path.with_suffix('.txt')
    
    doc = Document(str(docx_path))
    
    lines = []
    
    # Extract paragraphs
    for para in doc.paragraphs:
        if para.text.strip():
            lines.append(para.text)
    
    # Extract tables
    for table in doc.tables:
        lines.append("")  # Empty line before table
        for row in table.rows:
            cells = [cell.text.replace('\n', ' ') for cell in row.cells]
            lines.append(' | '.join(cells))
        lines.append("")  # Empty line after table
    
    # Write output
    with open(output_path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines))
    
    return output_path


def convert_to_html(docx_path, output_path=None):
    """
    Convert DOCX to basic HTML.
    
    Args:
        docx_path: Path to input DOCX file
        output_path: Optional output path (default: same name with .html)
    
    Returns:
        Path to generated HTML file
    """
    docx_path = Path(docx_path)
    
    if output_path:
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
    else:
        output_path = docx_path.with_suffix('.html')
    
    doc = Document(str(docx_path))
    
    html_parts = [
        '<!DOCTYPE html>',
        '<html>',
        '<head>',
        '<meta charset="utf-8">',
        f'<title>{docx_path.stem}</title>',
        '<style>',
        'body { font-family: Arial, sans-serif; max-width: 800px; margin: 0 auto; padding: 20px; }',
        'h1, h2, h3 { color: #333; }',
        'table { border-collapse: collapse; width: 100%; margin: 20px 0; }',
        'th, td { border: 1px solid #ddd; padding: 8px; text-align: left; }',
        'th { background-color: #f5f5f5; }',
        '</style>',
        '</head>',
        '<body>',
    ]
    
    # Convert paragraphs
    for para in doc.paragraphs:
        if not para.text.strip():
            continue
        
        style_name = para.style.name
        
        if style_name == 'Title':
            html_parts.append(f'<h1>{escape_html(para.text)}</h1>')
        elif style_name.startswith('Heading 1'):
            html_parts.append(f'<h1>{escape_html(para.text)}</h1>')
        elif style_name.startswith('Heading 2'):
            html_parts.append(f'<h2>{escape_html(para.text)}</h2>')
        elif style_name.startswith('Heading 3'):
            html_parts.append(f'<h3>{escape_html(para.text)}</h3>')
        elif style_name.startswith('List Bullet'):
            html_parts.append(f'<li>{escape_html(para.text)}</li>')
        elif style_name.startswith('List Number'):
            html_parts.append(f'<li>{escape_html(para.text)}</li>')
        else:
            html_parts.append(f'<p>{escape_html(para.text)}</p>')
    
    # Convert tables
    for table in doc.tables:
        html_parts.append('<table>')
        for i, row in enumerate(table.rows):
            html_parts.append('<tr>')
            tag = 'th' if i == 0 else 'td'
            for cell in row.cells:
                html_parts.append(f'<{tag}>{escape_html(cell.text)}</{tag}>')
            html_parts.append('</tr>')
        html_parts.append('</table>')
    
    html_parts.extend(['</body>', '</html>'])
    
    with open(output_path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(html_parts))
    
    return output_path


def escape_html(text):
    """Escape HTML special characters."""
    return (text
            .replace('&', '&amp;')
            .replace('<', '&lt;')
            .replace('>', '&gt;')
            .replace('"', '&quot;'))


def convert_to_images(docx_path, output_dir, format='png', dpi=150):
    """
    Convert DOCX to images (one per page).
    
    Args:
        docx_path: Path to input DOCX file
        output_dir: Directory for output images
        format: Image format (png, jpg)
        dpi: Resolution in DPI
    
    Returns:
        List of generated image paths
    """
    # First convert to PDF
    with tempfile.TemporaryDirectory() as temp_dir:
        temp_pdf = Path(temp_dir) / 'temp.pdf'
        convert_to_pdf(docx_path, temp_pdf)
        
        # Then convert PDF to images using pdftoppm
        pdftoppm = shutil.which('pdftoppm')
        if not pdftoppm:
            raise RuntimeError(
                "pdftoppm not found. Please install poppler-utils.\n"
                "  Windows: choco install poppler\n"
                "  macOS: brew install poppler\n"
                "  Linux: apt install poppler-utils"
            )
        
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        
        prefix = Path(docx_path).stem
        output_prefix = output_dir / prefix
        
        fmt_arg = '-png' if format == 'png' else '-jpeg'
        
        cmd = [
            pdftoppm,
            fmt_arg,
            '-r', str(dpi),
            str(temp_pdf),
            str(output_prefix)
        ]
        
        try:
            subprocess.run(cmd, capture_output=True, check=True)
        except subprocess.CalledProcessError as e:
            raise RuntimeError(f"pdftoppm conversion failed: {e.stderr}")
        
        # Find generated images
        ext = 'png' if format == 'png' else 'jpg'
        images = sorted(output_dir.glob(f'{prefix}-*.{ext}'))
        
        return [str(img) for img in images]


def batch_convert(input_dir, output_dir, format):
    """
    Batch convert all DOCX files in a directory.
    
    Args:
        input_dir: Input directory
        output_dir: Output directory
        format: Target format (pdf, txt, html)
    
    Returns:
        List of (input, output) tuples
    """
    input_path = Path(input_dir)
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    
    docx_files = list(input_path.glob('**/*.docx'))
    results = []
    
    for i, docx_file in enumerate(docx_files):
        # Preserve directory structure
        relative = docx_file.relative_to(input_path)
        
        if format == 'png' or format == 'jpg':
            out_dir = output_path / relative.parent / docx_file.stem
            out_file = out_dir
        else:
            out_file = output_path / relative.parent / (docx_file.stem + f'.{format}')
        
        print(f"[{i+1}/{len(docx_files)}] Converting: {docx_file.name}")
        
        try:
            if format == 'pdf':
                convert_to_pdf(docx_file, out_file)
            elif format == 'txt':
                convert_to_txt(docx_file, out_file)
            elif format == 'html':
                convert_to_html(docx_file, out_file)
            elif format in ('png', 'jpg'):
                convert_to_images(docx_file, out_file, format)
            
            results.append((str(docx_file), str(out_file)))
        except Exception as e:
            print(f"  Error: {e}")
    
    return results


def main():
    parser = argparse.ArgumentParser(
        description="Convert Word documents to other formats",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__
    )
    parser.add_argument("file", nargs="?", help="Input DOCX file")
    parser.add_argument("--dir", help="Input directory for batch conversion")
    parser.add_argument("--to", required=True, 
                        choices=['pdf', 'txt', 'html', 'png', 'jpg'],
                        help="Target format")
    parser.add_argument("-o", "--output", help="Output file or directory")
    parser.add_argument("--dpi", type=int, default=150,
                        help="DPI for image conversion (default: 150)")
    
    args = parser.parse_args()
    
    if not args.file and not args.dir:
        parser.error("Either provide an input file or use --dir")
    
    if args.dir:
        # Batch conversion
        if not args.output:
            args.output = str(Path(args.dir) / f'{args.to}_output')
        
        results = batch_convert(args.dir, args.output, args.to)
        print(f"\nConverted {len(results)} files to: {args.output}")
    
    else:
        # Single file conversion
        input_path = Path(args.file)
        if not input_path.exists():
            print(f"Error: File not found: {input_path}")
            sys.exit(1)
        
        try:
            if args.to == 'pdf':
                output = convert_to_pdf(input_path, args.output)
                print(f"PDF generated: {output}")
            
            elif args.to == 'txt':
                output = convert_to_txt(input_path, args.output)
                print(f"Text file generated: {output}")
            
            elif args.to == 'html':
                output = convert_to_html(input_path, args.output)
                print(f"HTML file generated: {output}")
            
            elif args.to in ('png', 'jpg'):
                output_dir = args.output or f'./{input_path.stem}_images'
                images = convert_to_images(input_path, output_dir, args.to, args.dpi)
                print(f"Generated {len(images)} images in: {output_dir}")
        
        except RuntimeError as e:
            print(f"Error: {e}")
            sys.exit(1)


if __name__ == "__main__":
    main()

