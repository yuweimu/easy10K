"""Convert PDF documents to Markdown while preserving page boundaries."""

import argparse
from pathlib import Path
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
        **markdown_options: Any,
    ) -> None:
        self.pdf_path = Path(pdf_path)
        self.output_path = (
            Path(output_path)
            if output_path is not None
            else self.pdf_path.with_suffix(".md")
        )
        self.markdown_options = markdown_options

    def convert(self, pages: list = None) -> Path:
        """Convert the PDF and return the path of the generated Markdown file."""

        if not self.pdf_path.is_file():
            raise FileNotFoundError(f"PDF file not found: {self.pdf_path}")

        total_pages = 0
        if pages:
            total_pages = pages.count
        else:
            with pymupdf.open(self.pdf_path) as document:
                total_pages = document.page_count
                pages = list(range(total_pages))

        options = dict(self.markdown_options)
        # options["page_chunks"] = True

        markdown_pages: list[str] = []

        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        self.output_path.write_text(
            f"<!-- GENERATED FROM PDF FILE: {self.pdf_path} -->\n\n",
            encoding="utf-8",
        )
        md_file = self.output_path.open("a", encoding="utf-8")

        for page_number in pages:
            print(
                f"\rConverting page {page_number + 1}/{total_pages}...",
                end="",
                flush=True,
            )
            # print(f"Digesting page {page_number + 1}/{total_pages} ...")
            page_markdown = pymupdf4llm.to_markdown(self.pdf_path, **options, pages = [page_number])
            
            md_file.write(f"<!-- PDF_PAGE: {page_number+1}/{total_pages} -->\n\n")
            md_file.write(page_markdown)
        print(" done.")

        return self.output_path

    # ``generate`` is a convenient alias for callers that think of this as a
    # document-generation operation rather than a format conversion.
    generate = convert


def main() -> None:
    """Run PDF-to-Markdown conversion from the command line."""

    parser = argparse.ArgumentParser(
        description="Convert a PDF to Markdown with searchable page markers."
    )
    parser.add_argument("pdf_path", type=Path, help="Path to the input PDF file")
    parser.add_argument(
        "-o",
        "--output",
        dest="output_path",
        type=Path,
        help="Path for the generated Markdown file (defaults to the PDF path with .md)",
    )
    args = parser.parse_args()

    output_path = PDF2MarkDown(args.pdf_path, args.output_path).convert()
    print(f"Generated Markdown: {output_path}")


if __name__ == "__main__":
    main()
