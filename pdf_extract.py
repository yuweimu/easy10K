"""Extract identifiers and consolidated financial statements from text PDFs.

Usage: python pdf_extract.py reports/宁德时代2025.pdf --output-dir extracted/pdf
Files and directories may be mixed; directories are searched recursively.
Without --output-dir, prints one JSON object for a single PDF or an array for a batch.
Amounts remain strings so their original precision and units are preserved.
"""

import argparse
from contextlib import redirect_stdout
from dataclasses import asdict, dataclass, field
import hashlib
import json
import os
from pathlib import Path
import re
import sys

import pymupdf

from report_metadata import match_dates, match_stock_code, match_title_reporting_date
from line_item_storage import LineItemStorage


STATEMENTS = {
    "合并资产负债表": "balance_sheet",
    "合并利润表": "income_statement",
    "合并现金流量表": "cash_flow_statement",
    "合并年初到报告期末利润表": "income_statement",
    "合并年初到报告期末现金流量表": "cash_flow_statement",
    "合并年初至报告期末利润表": "income_statement",
    "合并年初至报告期末现金流量表": "cash_flow_statement",
}
STATEMENT_FIELDS = tuple(dict.fromkeys(STATEMENTS.values()))
HEADING = re.compile(
    r"^(?:[\d一二三四五六七八九十]+[、.．])?"
    r"((?:合并|母公司)(?:资产负债表|(?:本报告期|年初[到至]报告期末)?利润表|"
    r"(?:年初[到至]报告期末)?现金流量表|所有者权益变动表|股东权益变动表))"
    r"(?:[（(]续[）)])?$"
)


def statement_title(text: str, following_lines: list[str] | tuple[str, ...] = ()) -> str | None:
    """Require a standalone heading with statement metadata in the next three lines."""
    lines = text.strip().splitlines()
    if len(lines) != 1:
        return None
    match = HEADING.fullmatch(re.sub(r"[\s*#]", "", text))
    if not match:
        return None
    title = match.group(1)
    context = re.sub(r"[\s*#]", "", "".join(following_lines[:3]))
    if re.search(r"(?:编制单位[：:]?.+|币种[：:（(]?人民币|(?<!编制)单位[：:（(]?(?:人民币)?[十百千万亿]*元)", context):
        return title
    if "资产负债表" in title:
        period = r"(?<!\d)20\d{2}(?:年\d{1,2}月\d{1,2}日|-\d{1,2}-\d{1,2})(?!\d)"
    elif "利润表" in title or "现金流量表" in title:
        period = r"(?<!\d)20\d{2}年\d{1,2}月?[—–－\-~～至到]\d{1,2}月"
    else:
        return None
    return title if re.search(period, context) else None


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
    title_reporting_date: str = ""
    balance_sheet_reporting_date: str = ""
    reporting_dates_match: bool | None = None


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
            if page_number <= 3 and not result.title_reporting_date:
                result.title_reporting_date = match_title_reporting_date(page.get_text(sort=True)) or ""
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
            boundaries = []
            for index, (bbox, text) in enumerate(lines):
                compact = re.sub(r"\s+", "", text)
                if any(marker in compact for marker in ("法定代表人", "公司负责人", "会计工作负责人", "会计机构负责人")):
                    boundaries.append((bbox, None))
                elif (title := statement_title(text, [line for _, line in lines[index + 1:index + 4]])) is not None:
                    boundaries.append((bbox, title))
            top = 0.0

            def collect(bottom: float) -> None:
                nonlocal active
                if active is None or bottom <= top:
                    return
                clip = pymupdf.Rect(0, top, page.rect.width, bottom)
                # Detect tables before clipping the final line so its bottom border
                # and amount cells remain available to the table extractor.
                # Ignore filled text-cell rectangles to avoid spurious columns.
                # PyMuPDF may print advisory messages; keep JSON stdout clean.
                with redirect_stdout(sys.stderr):
                    tables = page.find_tables(clip=clip, strategy="lines_strict")
                    table_data = [(table, table.extract()) for table in tables.tables]
                ending = None
                marker = {
                    "cash_flow_statement": "期末现金及现金等价物余额",
                    "income_statement": "稀释每股收益",
                    "balance_sheet": r"负债和.{0,6}权益",
                }.get(STATEMENTS[active.title])
                if marker:
                    ending = min((bbox[3] for bbox, line in lines
                                  if top <= bbox[1] and bbox[3] <= bottom
                                  and re.search(marker, re.sub(r"\s+", "", line))), default=None)
                    for table, extracted in table_data:
                        for row, cells in zip(table.rows, extracted):
                            if re.search(marker, re.sub(r"\s+", "", "".join(cell or "" for cell in cells))):
                                if ending is None or row.bbox[1] <= ending <= row.bbox[3]:
                                    ending = row.bbox[3]
                    if ending is not None:
                        clip.y1 = ending
                text = page.get_text(clip=clip, sort=True).strip()
                if not text:
                    return
                if page_number not in active.pages:
                    active.pages.append(page_number)
                active.text += text + "\n"
                if active.title == "合并资产负债表" and not result.balance_sheet_reporting_date:
                    dates = match_dates(text)
                    if dates:
                        result.balance_sheet_reporting_date = dates[0]
                for table, extracted in table_data:
                    retained = [cells for row, cells in zip(table.rows, extracted)
                                if ending is None or row.bbox[1] < ending]
                    active.rows.extend(clean_rows(retained))
                if ending is not None:
                    active = None

            for bbox, title in boundaries:
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
                    active = None  # Parent-company/equity headings and sign-offs end collection.
                top = bbox[3]
            collect(page.rect.height)
    result.reporting_date = result.balance_sheet_reporting_date or result.title_reporting_date
    if result.balance_sheet_reporting_date and result.title_reporting_date:
        result.reporting_dates_match = result.balance_sheet_reporting_date == result.title_reporting_date
    if not result.stock_code:
        # Prefer the filename, then the nearest parent directory containing a code.
        for component in reversed(path.parts):
            code = re.search(r"(?<!\d)((?:00|60|30|68)\d{4})(?!\d)", component)
            if code:
                result.stock_code = code.group(1)
                break
    return result


def collect_pdf_paths(inputs: list[Path]) -> list[Path]:
    """Expand directories recursively and deduplicate overlapping PDF inputs."""
    files = {}
    for path in inputs:
        if path.is_dir():
            candidates = sorted(candidate for candidate in path.rglob("*")
                                if candidate.is_file() and candidate.suffix.lower() == ".pdf")
        elif path.is_file() and path.suffix.lower() == ".pdf":
            candidates = [path]
        else:
            raise ValueError(f"Not a PDF file or directory: {path}")
        for candidate in candidates:
            resolved = candidate.resolve()
            files.setdefault(resolved, None)
    if not files:
        raise ValueError("No PDF files found in the supplied paths")
    return list(files)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pdf_path", type=Path, nargs="+",
                        help="PDF files or directories to search recursively")
    parser.add_argument("--output-dir", "-o", type=Path,
                        help="Write JSON and Markdown; preserve relative folders for multiple PDFs")
    args = parser.parse_args()
    try:
        files = collect_pdf_paths(args.pdf_path)
    except ValueError as error:
        parser.error(str(error))
    root = Path(os.path.commonpath([path.parent for path in files]))
    failed = False
    with LineItemStorage() as storage:
        for path in files:
            pdf_hash = None
            try:
                with path.open("rb") as source:
                    pdf_hash = hashlib.file_digest(source, "md5").hexdigest()
                stage = storage.get_report_digestion_stage(pdf_hash)
                if stage in ("AuditPass", "ManualPass"):
                    print(f"Skipping {path}({pdf_hash}): {stage}", file=sys.stderr)
                    continue
                print(f"Extracting {path}({pdf_hash})")
                result = extract_report(path)
                data = asdict(result)
                payload = json.dumps(data, ensure_ascii=False, indent=2)
                markdown = {
                    name: statement.markdown() if (statement := getattr(result, name)) is not None else None
                    for name in STATEMENT_FIELDS
                }
                if args.output_dir:
                    destination = args.output_dir / path.relative_to(root).with_suffix("")
                    destination.mkdir(parents=True, exist_ok=True)
                    (destination / "report.json").write_text(payload + "\n", encoding="utf-8")
                    for name in STATEMENT_FIELDS:
                        if markdown[name] is not None:
                            (destination / f"{name}.md").write_text(markdown[name], encoding="utf-8")
                storage.store_extracted_report(
                    result.pdf_hash, payload, markdown["balance_sheet"], markdown["income_statement"],
                    markdown["cash_flow_statement"], local_path=result.pdf_file,
                    stock_code=result.stock_code, reporting_date=result.reporting_date,
                    pdf_creation_date=result.pdf_creation_time,
                )
                if args.output_dir:
                    print(destination)
                else:
                    for label, name in zip(("BS", "IS", "CF"), STATEMENT_FIELDS):
                        statement = getattr(result, name)
                        if statement is None:
                            print(f"\t{label} Not extracted")
                        else:
                            print(f"\t{label} {statement.title}\t@ {statement.pages}")
            except Exception as error:
                failed = True
                print(f"{path}: {error}", file=sys.stderr)
                # A failed database write must not poison the next report's transaction.
                storage.close()
                if pdf_hash is not None and storage.database_url:
                    try:
                        storage.update_report_digestion_stage(pdf_hash, "ExtractionError")
                    except Exception as stage_error:
                        print(f"{path}: unable to save ExtractionError: {stage_error}", file=sys.stderr)
                        storage.close()
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
