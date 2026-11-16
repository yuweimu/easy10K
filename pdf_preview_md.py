"""Convert a PDF report to readable Markdown, without database dependencies.

Retains detected headings, emphasis, tables, reading order, and page boundaries.
Markdown cannot reproduce exact PDF positioning; images and charts are omitted.
Scanned PDFs require OCR before conversion.
"""

import argparse
from html import escape
import os
from pathlib import Path
import tempfile

import pymupdf
import pymupdf4llm


def convert_pdf_to_markdown(
    pdf_path: str | Path,
    output: str | Path | None = None,
    *,
    start_page: int = 1,
    end_page: int | None = None,
    force: bool = False,
    table_strategy: str = "lines_strict",
) -> Path:
    """Publish a completed Markdown file atomically; page ranges are inclusive."""
    source = Path(pdf_path).resolve()
    if not source.is_file():
        raise FileNotFoundError(f"PDF file not found: {source}")
    destination = Path(output).resolve() if output is not None else source.with_suffix(".md")
    if destination == source:
        raise ValueError("Markdown output must not overwrite the source PDF")
    if destination.exists() and not force:
        raise FileExistsError(f"Output exists; use --force to replace it: {destination}")
    if table_strategy not in {"lines_strict", "lines", "text"}:
        raise ValueError("table_strategy must be lines_strict, lines, or text")

    temporary = None
    try:
        with pymupdf.open(source) as document:
            if document.needs_pass:
                raise ValueError("Password-protected PDF: decrypt it before conversion")
            total = len(document)
            last = total if end_page is None else end_page
            if not 1 <= start_page <= last <= total:
                raise ValueError(f"Page range must satisfy 1 <= start <= end <= {total}")
            pages = list(range(start_page - 1, last))
            # Analyze heading sizes once, not once for every extracted page.
            headers = pymupdf4llm.IdentifyHeaders(document, pages=pages)
            destination.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=destination.parent,
                prefix=f".{destination.name}.", suffix=".tmp", delete=False,
            ) as writer:
                temporary = Path(writer.name)
                writer.write(f"# {escape(source.stem)}\n\n")
                writer.write("<!-- Images/charts omitted; layout is approximate. -->\n\n")
                writer.write("## Contents / 目录\n\n")
                bookmarks = [entry for entry in document.get_toc() if start_page <= entry[2] <= last]
                if bookmarks:
                    for level, title, number in bookmarks:
                        writer.write(
                            f'{"  " * min(max(level - 1, 0), 5)}- '
                            f'<a href="#pdf-page-{number}">{escape(title)}</a>\n'
                        )
                else:
                    for number in range(start_page, last + 1):
                        writer.write(f'- [Page {number}](#pdf-page-{number})\n')
                for index in pages:
                    number = index + 1
                    writer.write(
                        f'\n---\n\n<a id="pdf-page-{number}"></a>\n\n'
                        f'<!-- PDF_PAGE: {number}/{total} -->\n\n'
                        f'### Page {number} / {total}\n\n'
                    )
                    markdown = pymupdf4llm.to_markdown(
                        document, pages=[index], hdr_info=headers,
                        write_images=False, embed_images=False, force_text=True,
                        table_strategy=table_strategy, show_progress=False,
                    ).strip()
                    writer.write(markdown or "*No extractable text on this page; OCR may be required.*")
                    writer.write("\n")
                writer.flush()
                os.fsync(writer.fileno())
        os.replace(temporary, destination)
        return destination
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pdf_path", type=Path)
    parser.add_argument("--output", "-o", type=Path, help="Output Markdown (default: beside PDF)")
    parser.add_argument("--start-page", type=int, default=1, help="First page, one-based")
    parser.add_argument("--end-page", type=int, help="Last page, inclusive")
    parser.add_argument("--force", action="store_true", help="Replace existing Markdown")
    parser.add_argument("--table-strategy", choices=["lines_strict", "lines", "text"],
                        default="lines_strict", help="Try text for tables without visible borders")
    args = parser.parse_args(argv)
    try:
        result = convert_pdf_to_markdown(
            args.pdf_path, args.output, start_page=args.start_page,
            end_page=args.end_page, force=args.force, table_strategy=args.table_strategy,
        )
    except (OSError, ValueError, RuntimeError) as error:
        parser.exit(1, f"Error: {error}\n")
    print(result)


if __name__ == "__main__":
    main()
