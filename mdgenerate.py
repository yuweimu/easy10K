"""Convert PDF documents to Markdown while preserving page boundaries."""

import argparse
import hashlib
import os
from pathlib import Path
import re
import tempfile
from typing import Any

import pymupdf
import pymupdf4llm

from line_item_storage import LineItemStorage
from report_metadata import extract_report_identifiers


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
        self._default_output = output_path is None
        self.output_path = Path(output_path) if output_path is not None else Path("reports/markdown")
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
                destination = None if self._default_output else self.output_path / source.relative_to(self.pdf_path).with_suffix(".md")
                converter = PDF2MarkDown(source, destination, force=self.force, **self.markdown_options)
                outputs.append(converter._convert_file(pages))
            return outputs
        return self._convert_file(pages)

    def _convert_file(self, pages: list[int] | None = None) -> Path:
        """Convert a single file without sharing document state across reports."""
        if not self.pdf_path.is_file():
            raise FileNotFoundError(f"PDF file not found: {self.pdf_path}")
        if not self._default_output and self.output_path.is_file() and not self.force:
            print(f"Skipped existing Markdown: {self.output_path.resolve()}")
            return self.output_path

        with pymupdf.open(self.pdf_path) as document:
            pdf_creation_time = self.get_pdf_creation_date(document)
            if self._default_output:
                stock_code, reporting_date = extract_report_identifiers(
                    "\n".join(page.get_text() for page in document)
                )
                if not stock_code or not reporting_date or not isinstance(pdf_creation_time, str) or not pdf_creation_time.strip():
                    raise ValueError(
                        f"Cannot determine default output for {self.pdf_path}: "
                        "stock_code, reporting_date and PDF creation time are required; specify --output"
                    )
                if any(character in pdf_creation_time for character in ("/", "\\", "\0")):
                    raise ValueError("PDF creation time is not safe for a filename; specify --output")
                self.output_path = Path("reports/markdown") / stock_code / f"{stock_code}.{reporting_date}.{pdf_creation_time}.md"
                if self.output_path.is_file() and not self.force:
                    print(f"Skipped existing Markdown: {self.output_path.resolve()}")
                    return self.output_path

            with self.pdf_path.open("rb") as pdf_file:
                pdf_hash = hashlib.file_digest(
                    pdf_file, lambda: hashlib.md5(usedforsecurity=False),
                ).hexdigest()
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
                        f"<!-- ORIGINAL_PDF_FILE_DOWNLOAD_URL:local -->\n"
                        f"<!-- ORIGINAL_PDF_FILE:{self.pdf_path.resolve()} -->\n"
                        f"<!-- ORIGINAL_PDF_FILE_HASH:{pdf_hash} -->\n"
                        f"<!-- ORIGINAL_PDF_FILE_CREATION_DATE:{pdf_creation_time} -->\n"
                    )
                    for page_number in pages:
                        print(
                            f"\rConverting {self.pdf_path.resolve()}: {page_number + 1}/{total_pages} " + "." * (page_number % 6 + 1) + " " * (5 - page_number % 6),
                            end="",
                            flush=True,
                        )
                        page_markdown = pymupdf4llm.to_markdown(document, **options, pages=[page_number])
                        md_file.write(f"<!-- PDF_PAGE: {page_number+1}/{total_pages} -->\n")
                        md_file.write(page_markdown)
                if self.force:
                    temporary_path.replace(self.output_path)
                else:
                    # A hard link publishes atomically without overwriting a concurrent output.
                    try:
                        os.link(temporary_path, self.output_path)
                    except FileExistsError:
                        print(f"Skipped existing Markdown: {self.output_path.resolve()}")
                        return self.output_path
            finally:
                if temporary_path is not None:
                    temporary_path.unlink(missing_ok=True)
            if isinstance(pdf_creation_time, str) and pdf_creation_time.strip():
                with LineItemStorage() as storage:
                    storage.store_report_profile(
                        pdf_hash=pdf_hash,
                        pdf_creation_time=pdf_creation_time,
                        pdf_download_url="local",
                        pdf_local_path=str(self.pdf_path.resolve()),
                        markdown_file=str(self.output_path.resolve()),
                        preprocessed_file=None,
                    )
            else:
                print(f"Skipped registry entry (missing PDF creation time): {self.pdf_path.resolve()}")
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
        help="Output file for a single PDF, or output directory preserving subfolders for directory input; default: ./reports/markdown/{stock_code}/{stock_code}.{reporting_date}.{pdf_creation_time}.md",
    )
    args = parser.parse_args()

    output = PDF2MarkDown(args.pdf_path, args.output_path, force=args.force).convert()
    for output_path in output if isinstance(output, list) else [output]:
        print(f"Markdown: {output_path.resolve()}")


if __name__ == "__main__":
    main()
