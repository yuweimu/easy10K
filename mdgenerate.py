"""Convert PDF documents to Markdown while preserving page boundaries."""

import argparse
import hashlib
import os
from pathlib import Path
import re
import shutil
import sys
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

    def convert(self, pages: list[int] | None = None) -> Path | list[Path] | None:
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
                output = converter._convert_file(pages)
                if output is not None:
                    outputs.append(output)
            return outputs
        return self._convert_file(pages)

    def _convert_file(self, pages: list[int] | None = None) -> Path | None:
        """Convert a single file without sharing document state across reports."""
        if not self.pdf_path.is_file():
            raise FileNotFoundError(f"PDF file not found: {self.pdf_path}")
        context: dict[str, Any] = {}
        try:
            return self._convert_file_impl(pages, context)
        except Exception as exc:
            print(f"Skipped {self.pdf_path.resolve()}: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
            diagnostic = self._save_failed_markdown(context, exc)
            creation = context.get("pdf_creation_time")
            if context.get("pdf_hash") and isinstance(creation, str) and creation.strip():
                try:
                    with LineItemStorage() as storage:
                        storage.store_report_profile(
                            pdf_hash=context["pdf_hash"], pdf_creation_time=creation,
                            pdf_download_url="local", pdf_local_path=str(self.pdf_path.resolve()),
                            markdown_file=str(diagnostic.resolve()) if diagnostic else None,
                            preprocessed_file=None,
                        )
                        storage.update_digestion_status(context["pdf_hash"], creation, "PDF2MD_ERROR")
                except Exception as registry_error:
                    print(f"Unable to record PDF2MD_ERROR: {registry_error}", file=sys.stderr)
            else:
                print("Cannot record PDF2MD_ERROR: PDF hash or creation time is missing", file=sys.stderr)
            return None
        finally:
            temporary_path = context.get("temporary_path")
            if temporary_path is not None:
                try:
                    temporary_path.unlink(missing_ok=True)
                except OSError as cleanup_error:
                    print(f"Unable to clean up {temporary_path}: {cleanup_error}", file=sys.stderr)

    def _save_failed_markdown(self, context: dict[str, Any], error: Exception) -> Path | None:
        """Publish available Markdown beside the PDF, backing up any existing copy."""
        destination = self.pdf_path.with_suffix(".md")
        temporary: Path | None = None
        try:
            source = context.get("published_path") or context.get("temporary_path")
            with tempfile.NamedTemporaryFile(
                mode="wb", dir=destination.parent, prefix=f".{destination.name}.", suffix=".tmp", delete=False,
            ) as output:
                temporary = Path(output.name)
                if source is not None and source.is_file():
                    with source.open("rb") as markdown:
                        shutil.copyfileobj(markdown, output)
                else:
                    output.write((
                        f"<!-- ORIGINAL_PDF_FILE:{self.pdf_path.resolve()} -->\n"
                        f"<!-- ORIGINAL_PDF_FILE_HASH:{context.get('pdf_hash', '')} -->\n"
                        f"<!-- ORIGINAL_PDF_FILE_CREATION_DATE:{context.get('pdf_creation_time', '')} -->\n"
                        f"\nConversion failed before Markdown was available: {type(error).__name__}: {error}\n"
                    ).encode("utf-8"))
            if destination.exists():
                with tempfile.NamedTemporaryFile(
                    dir=destination.parent, prefix=f"{destination.stem}.previous-", suffix=".md", delete=False,
                ) as backup:
                    backup_path = Path(backup.name)
                shutil.copy2(destination, backup_path)
                print(f"Backed up existing Markdown: {backup_path.resolve()}", file=sys.stderr)
            temporary.replace(destination)
            print(f"Saved investigation Markdown: {destination.resolve()}", file=sys.stderr)
            return destination
        except Exception as save_error:
            print(f"Unable to save investigation Markdown: {save_error}", file=sys.stderr)
            # Keep the original conversion temporary file if the fallback could not be saved.
            retained = context.pop("temporary_path", None)
            if retained is not None and retained.is_file():
                print(f"Retained conversion temporary file: {retained.resolve()}", file=sys.stderr)
            return None
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass

    def _convert_file_impl(self, pages: list[int] | None, context: dict[str, Any]) -> Path | None:
        if not self._default_output and self.output_path.is_file() and not self.force:
            print(f"Skipped existing Markdown: {self.output_path.resolve()}")
            return self.output_path

        with pymupdf.open(self.pdf_path) as document:
            pdf_creation_time = self.get_pdf_creation_date(document)
            context["pdf_creation_time"] = pdf_creation_time
            with self.pdf_path.open("rb") as pdf_file:
                pdf_hash = hashlib.file_digest(
                    pdf_file, lambda: hashlib.md5(usedforsecurity=False),
                ).hexdigest()
            context["pdf_hash"] = pdf_hash
            previous_status = None
            if isinstance(pdf_creation_time, str) and pdf_creation_time.strip():
                with LineItemStorage() as storage:
                    previous_status = storage.get_report_digestion_status(pdf_hash, pdf_creation_time)
                if not self.force and previous_status is not None and previous_status != "PDF2MD_ERROR":
                    print(f"Skipped registered PDF ({previous_status}): {self.pdf_path.resolve()}")
                    return None
            total_pages = document.page_count
            pages = list(range(total_pages)) if pages is None else list(pages)

            options = dict(self.markdown_options)
            staging_dir = Path("reports/markdown") if self._default_output else self.output_path.parent
            staging_dir.mkdir(parents=True, exist_ok=True)
            temporary_path: Path | None = None
            try:
                # Legacy extraction otherwise scans the entire PDF on every page call.
                if options.get("hdr_info") is None and hasattr(pymupdf4llm, "IdentifyHeaders"):
                    options["hdr_info"] = pymupdf4llm.IdentifyHeaders(document)
                # Use the destination filesystem so publishing the completed file is atomic.
                with tempfile.NamedTemporaryFile(
                    mode="w", encoding="utf-8", dir=staging_dir,
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
                if self._default_output:
                    # Name the report only after Markdown extraction has completed.
                    stock_code, reporting_date = extract_report_identifiers(
                        temporary_path.read_text(encoding="utf-8")
                    )
                    reporting_date = reporting_date or "no-date"
                    if not stock_code or not isinstance(pdf_creation_time, str) or not pdf_creation_time.strip():
                        raise ValueError(
                            f"Cannot determine default output for {self.pdf_path}: "
                            "stock_code and PDF creation time are required; specify --output"
                        )
                    if any(character in pdf_creation_time for character in ("/", "\\", "\0")):
                        raise ValueError("PDF creation time is not safe for a filename; specify --output")
                    self.output_path = Path("reports/markdown") / stock_code / f"{stock_code}.{reporting_date}.{pdf_creation_time}.md"
                    self.output_path.parent.mkdir(parents=True, exist_ok=True)
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
                context["temporary_path"] = temporary_path
            context["published_path"] = self.output_path
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
                    if previous_status == "PDF2MD_ERROR":
                        storage.update_digestion_status(pdf_hash, pdf_creation_time, "PENDING")
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
    parser.add_argument("pdf_path", type=Path, nargs="?", default=Path("reports/久立特材2026q1.pdf"),
                        help="Input PDF file or directory to search recursively (default: reports/)")
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
    for output_path in output if isinstance(output, list) else [] if output is None else [output]:
        print(f"Markdown: {output_path.resolve()}")


if __name__ == "__main__":
    main()
