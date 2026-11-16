"""Extract identifiers and consolidated financial statements from text PDFs.

Usage: python pdf_extract.py reports/宁德时代2025.pdf --output-dir extracted/pdf
Without --output-dir, the complete extraction is printed as JSON.
Amounts remain strings so their original precision and units are preserved.
"""

import argparse
from contextlib import redirect_stdout
from dataclasses import asdict, dataclass, field
import hashlib
import json
from pathlib import Path
import re
import sys

import pymupdf

from report_metadata import match_dates, match_stock_code
from line_item_storage import LineItemStorage


STATEMENTS = {
    "合并资产负债表": "balance_sheet",
    "合并利润表": "income_statement",
    "合并现金流量表": "cash_flow_statement",
    "合并本报告期利润表": "income_statement",
    "合并年初到报告期末利润表": "income_statement",
    "合并年初到报告期末现金流量表": "cash_flow_statement",
}
STATEMENT_FIELDS = tuple(dict.fromkeys(STATEMENTS.values()))
HEADING = re.compile(
    r"^(?:[\d一二三四五六七八九十]+[、.．])?"
    r"((?:合并|母公司)(?:资产负债表|(?:本报告期|年初到报告期末)?利润表|"
    r"(?:年初到报告期末)?现金流量表|所有者权益变动表|股东权益变动表))"
    r"(?:[（(]续[）)])?$"
)


def statement_title(text: str) -> str | None:
    """Recognize standalone statement headings, not prose or TOC entries."""
    match = HEADING.fullmatch(re.sub(r"[\s*#]", "", text))
    return match.group(1) if match else None


def clean_rows(rows: list[list[str | None]]) -> list[list[str]]:
    """Remove detector-created empty columns, keeping empty amount cells."""
    if not rows:
        return []
    width = max(map(len, rows))
    cleaned = [
        [re.sub(r"\s+", " ", cell or "").strip() for cell in row]
        + [""] * (width - len(row))
        for row in rows
    ]
    columns = [i for i in range(width) if any(row[i] for row in cleaned)]
    return [[row[i] for i in columns] for row in cleaned if any(row)]


@dataclass
class Statement:
    title: str
    pages: list[int] = field(default_factory=list)
    rows: list[list[str]] = field(default_factory=list)
    text: str = ""

    def markdown(self) -> str:
        """Include the caption (notably currency units) and extracted tables."""
        # PDF text extraction can concatenate the caption and the whole table
        # into one line. Keep only caption values, never that complete line.
        captions = match_dates(self.text)
        captions.extend(re.findall(
            r"(?<!编制)单位\s*[:：]\s*(?:人民币\s*)?(?:亿元|万元|千元|元)",
            self.text,
        ))
        caption = "\n".join(dict.fromkeys(captions))
        parts = [f"# {self.title}", caption if self.rows else self.text.strip()]
        if self.rows:
            width = max(map(len, self.rows))
            rows = [row + [""] * (width - len(row)) for row in self.rows]
            def format_row(row: list[str]) -> str:
                return "| " + " | ".join(cell.replace("|", "\\|") for cell in row) + " |"
            parts.extend([format_row(rows[0]), format_row(["---"] * width)])
            parts.extend(format_row(row) for row in rows[1:])
        return "\n\n".join(parts[:2]) + "\n\n" + "\n".join(parts[2:]) + "\n"


@dataclass
class ReportExtraction:
    pdf_file: str
    pdf_hash: str = ""
    stock_code: str = ""
    reporting_date: str = ""
    pdf_creation_time: str = ""
    balance_sheet: Statement | None = None
    income_statement: Statement | None = None
    cash_flow_statement: Statement | None = None


def extract_report(pdf_path: str | Path) -> ReportExtraction:
    """Extract the first set of consolidated statements, including continuations.

    Page numbers are one-based. Text is retained alongside structured rows as a
    fallback for borderless/unrecognized tables. Scanned PDFs need OCR first.
    """
    path = Path(pdf_path).resolve()
    result = ReportExtraction(pdf_file=str(path))
    with path.open("rb") as source:
        result.pdf_hash = hashlib.file_digest(source, "md5").hexdigest()
    active: Statement | None = None
    with pymupdf.open(path) as document:
        raw_date = document.metadata.get("creationDate", "") or ""
        date = re.match(r"(?:D:)?(\d{14})", raw_date)
        result.pdf_creation_time = date.group(1) if date else raw_date
        for page_number, page in enumerate(document, 1):
            lines = []
            for block in page.get_text("dict", flags=pymupdf.TEXTFLAGS_TEXT)["blocks"]:
                for line in block.get("lines", []):
                    text = "".join(span["text"] for span in line["spans"])
                    lines.append((line["bbox"], text))
            lines.sort(key=lambda item: (item[0][1], item[0][0]))
            if not result.stock_code:
                # PDF cells may store the label and value in separate text lines.
                for text in page.get_text(sort=True).splitlines():
                    result.stock_code = match_stock_code(text) or ""
                    if result.stock_code:
                        break
            headings = [(bbox, statement_title(text)) for bbox, text in lines
                        if statement_title(text)]
            top = 0.0

            def collect(bottom: float) -> None:
                if active is None or bottom <= top:
                    return
                clip = pymupdf.Rect(0, top, page.rect.width, bottom)
                text = page.get_text(clip=clip, sort=True).strip()
                if not text:
                    return
                if page_number not in active.pages:
                    active.pages.append(page_number)
                active.text += text + "\n"
                if active.title == "合并资产负债表" and not result.reporting_date:
                    dates = match_dates(text)
                    if dates:
                        result.reporting_date = dates[0]
                # Ignore filled text-cell rectangles: otherwise they introduce
                # spurious columns in reports such as CATL's annual report.
                # PyMuPDF may print advisory messages; keep JSON stdout clean.
                with redirect_stdout(sys.stderr):
                    tables = page.find_tables(clip=clip, strategy="lines_strict")
                    for table in tables.tables:
                        active.rows.extend(clean_rows(table.extract()))

            for bbox, title in headings:
                collect(bbox[1])
                if title in STATEMENTS:
                    name = STATEMENTS[title]
                    existing = getattr(result, name)
                    if existing is None:
                        active = Statement(title)
                        setattr(result, name, active)
                    elif active is not existing or existing.title != title:
                        active = None  # Do not append a second copy of a statement.
                else:
                    active = None  # Parent-company/equity statements are boundaries.
                top = bbox[3]
            collect(page.rect.height)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pdf_path", type=Path)
    parser.add_argument("--output-dir", "-o", type=Path,
                        help="Write JSON and three separate Markdown files under PDF stem")
    args = parser.parse_args()
    result = extract_report(args.pdf_path)
    payload = json.dumps(asdict(result), ensure_ascii=False, indent=2)
    markdown = {
        name: statement.markdown() if (statement := getattr(result, name)) is not None else None
        for name in STATEMENT_FIELDS
    }
    if args.output_dir:
        destination = args.output_dir / args.pdf_path.stem
        destination.mkdir(parents=True, exist_ok=True)
        (destination / "report.json").write_text(payload + "\n", encoding="utf-8")
        for name in STATEMENT_FIELDS:
            if markdown[name] is not None:
                (destination / f"{name}.md").write_text(markdown[name], encoding="utf-8")
    with LineItemStorage() as storage:
        storage.store_extracted_report(
            result.pdf_hash, payload, markdown["balance_sheet"], markdown["income_statement"],
            markdown["cash_flow_statement"], local_path=result.pdf_file,
            stock_code=result.stock_code, reporting_date=result.reporting_date,
            pdf_creation_date=result.pdf_creation_time,
        )
    if args.output_dir:
        print(destination)
    else:
        print(payload)


if __name__ == "__main__":
    main()
