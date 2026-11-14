"""Convert PDF documents to Markdown while preserving page boundaries."""

import argparse
import os
from pathlib import Path
import re
import tempfile
from typing import Any

import pymupdf
import pymupdf4llm


class PDF2MarkDown:
    """Convert a PDF to a Markdown file with searchable page markers.

    Each page in the generated file starts with an HTML comment such as
    ``<!-- PDF_PAGE: 1 -->``.  HTML comments are easy to search for while
    remaining invisible when the Markdown is rendered.
    """

    def __init__(
        self,
        pdf_path: str | Path,
        output_path: str | Path | None = None,
        *,
        force: bool = False,
        **markdown_options: Any,
    ) -> None:
        self.pdf_path = Path(pdf_path)
        self.output_path = (
            Path(output_path)
            if output_path is not None
            else self.pdf_path if self.pdf_path.is_dir() else self.pdf_path.with_suffix(".md")
        )
        self.markdown_options = markdown_options
        self.force = force

    def get_pdf_creation_date(self, document=None):
        try:
            if document is None:
                with pymupdf.open(self.pdf_path) as opened_document:
                    return self.get_pdf_creation_date(opened_document)
            creation_date = (document.metadata or {}).get("creationDate") or ""

            m = re.match(r"D:(\d{14})", creation_date)
            # PDF 日期格式通常为 "D:YYYYMMDDHHmmss..."，这里简单返回原始字符串
            if m:
                return m.group(1)
            else:
                return creation_date
        except Exception as e:
            print(f"creationDate not found: {e}")
            return None

    def convert(self, pages: list[int] | None = None) -> Path | list[Path]:
        """Convert one PDF, or recursively convert a directory in sorted order."""
        if self.pdf_path.is_dir():
            if self.output_path.exists() and not self.output_path.is_dir():
                raise NotADirectoryError(f"Output must be a directory: {self.output_path}")
            sources = sorted(
                path for path in self.pdf_path.rglob("*")
                if path.is_file() and path.suffix.lower() == ".pdf"
            )
            outputs: list[Path] = []
            for source in sources:
                destination = self.output_path / source.relative_to(self.pdf_path).with_suffix(".md")
                converter = PDF2MarkDown(source, destination, force=self.force, **self.markdown_options)
                outputs.append(converter._convert_file(pages))
            return outputs
        return self._convert_file(pages)

    def _convert_file(self, pages: list[int] | None = None) -> Path:
        """Convert a single file without sharing document state across reports."""
        if not self.pdf_path.is_file():
            raise FileNotFoundError(f"PDF file not found: {self.pdf_path}")
        if self.output_path.is_file() and not self.force:
            print(f"Skipped existing Markdown: {self.output_path}")
            return self.output_path

        with pymupdf.open(self.pdf_path) as document:
            total_pages = document.page_count
            pages = list(range(total_pages)) if pages is None else list(pages)

            options = dict(self.markdown_options)
            # Legacy extraction otherwise scans the entire PDF on every page call.
            if options.get("hdr_info") is None and hasattr(pymupdf4llm, "IdentifyHeaders"):
                options["hdr_info"] = pymupdf4llm.IdentifyHeaders(document)

            self.output_path.parent.mkdir(parents=True, exist_ok=True)
            temporary_path: Path | None = None
            try:
                # Use the destination filesystem so publishing the completed file is atomic.
                with tempfile.NamedTemporaryFile(
                    mode="w", encoding="utf-8", dir=self.output_path.parent,
                    prefix=f".{self.output_path.name}.", suffix=".tmp", delete=False,
                ) as md_file:
                    temporary_path = Path(md_file.name)
                    md_file.write(
                        f"<!-- ORIGINAL_PDF_FILE:{self.pdf_path} -->\n\n"
                        f"<!-- ORIGINAL_PDF_FILE_CREATION_DATE:{self.get_pdf_creation_date(document)} -->\n\n"
                    )
                    for page_number in pages:
                        print(
                            f"\rConverting {self.pdf_path}: {page_number + 1}/{total_pages} " + "." * (page_number % 6 + 1) + " " * (5 - page_number % 6),
                            end="",
                            flush=True,
                        )
                        page_markdown = pymupdf4llm.to_markdown(document, **options, pages=[page_number])
                        md_file.write(f"<!-- PDF_PAGE: {page_number+1}/{total_pages} -->\n\n")
                        md_file.write(page_markdown)
                if self.force:
                    temporary_path.replace(self.output_path)
                else:
                    # A hard link publishes atomically without overwriting a concurrent output.
                    try:
                        os.link(temporary_path, self.output_path)
                    except FileExistsError:
                        print(f"Skipped existing Markdown: {self.output_path}")
                        return self.output_path
            finally:
                if temporary_path is not None:
                    temporary_path.unlink(missing_ok=True)
            print(" done.")

            return self.output_path

    # ``generate`` is a convenient alias for callers that think of this as a
    # document-generation operation rather than a format conversion.
    generate = convert


def main() -> None:
    """Run PDF-to-Markdown conversion from the command line."""

    parser = argparse.ArgumentParser(
        description="Convert a PDF or recursively convert a directory of PDFs to Markdown."
    )
    parser.add_argument("pdf_path", type=Path, help="Input PDF file or directory to search recursively")
    parser.add_argument(
        "--force", action="store_true", help="Overwrite existing Markdown files (otherwise skip them)",
    )
    parser.add_argument(
        "-o",
        "--output",
        dest="output_path",
        type=Path,
        help="Output file for a single PDF, or output directory preserving subfolders for directory input; defaults to beside each PDF",
    )
    args = parser.parse_args()

    output = PDF2MarkDown(args.pdf_path, args.output_path, force=args.force).convert()
    for output_path in output if isinstance(output, list) else [output]:
        print(f"Markdown: {output_path}")


if __name__ == "__main__":
    main()
