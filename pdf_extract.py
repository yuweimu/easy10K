"""Extract identifiers and consolidated financial statements from text PDFs.

Usage: python pdf_extract.py --output-dir extracted/pdf
Processes registered reports at the Downloaded stage using their local_path.
Without --output-dir, prints extraction progress and a statement summary per PDF.
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
    "利润表": "income_statement",
    "合并现金流量表": "cash_flow_statement",
    "合并年初到报告期末利润表": "income_statement",
    "合并年初到报告期末现金流量表": "cash_flow_statement",
    "合并年初至报告期末利润表": "income_statement",
    "合并年初至报告期末现金流量表": "cash_flow_statement",
    "合并及公司资产负债表": "balance_sheet",
    "合并及公司利润表": "income_statement",
    "合并及公司现金流量表": "cash_flow_statement",
    "合并及母公司资产负债表": "balance_sheet",
    "合并及母公司利润表": "income_statement",
    "合并及母公司现金流量表": "cash_flow_statement",
    "资产负债表": "balance_sheet",
    "现金流量表": "cash_flow_statement",
    "中期合并资产负债表": "balance_sheet",
    "中期合并利润表": "income_statement",
    "中期合并现金流量表": "cash_flow_statement",
}
STATEMENT_FIELDS = tuple(dict.fromkeys(STATEMENTS.values()))
PERIOD_END = r"截至20\d{2}年\d{1,2}月\d{1,2}日止(?:\d{1,2}|[一二三四五六七八九十两]{1,3})个月期间"
QUARTER_PERIOD = r"20\d{2}年第?[一二三四1-4]季度"
DATE_RANGE_PERIOD = r"20\d{2}年\d{1,2}月\d{1,2}日[至到](?:20\d{2}年)?\d{1,2}月\d{1,2}日止期间"
COMPARATIVE_PERIOD = (
    r"(?:截至20\d{2}年\d{1,2}月\d{1,2}日[和及]20\d{2}年\d{1,2}月\d{1,2}日|"
    r"20\d{2}年\d{1,2}[—–－\-~～至到]\d{1,2}月[和及]20\d{2}年\d{1,2}[—–－\-~～至到]\d{1,2}月)的"
)
HEADING = re.compile(
    r"^(?:[\d一二三四五六七八九十]+(?:-[\d一二三四五六七八九十]+)*[、.．]|"
    r"[（(][\d一二三四五六七八九十]+[）)])?"
    rf"(?:{COMPARATIVE_PERIOD}|{PERIOD_END}|{DATE_RANGE_PERIOD}|20\d{{2}}年\d{{1,2}}月\d{{1,2}}日|20\d{{2}}年度|{QUARTER_PERIOD})?"
    r"(?:未经审计)?"
    r"((?:(?:中期)?合并(?:及(?:母公司|公司))?|合共及公司|母公司)(?:资产负债表|(?:本报告期|年初[到至]报告期末)?利润表|"
    r"(?:年初[到至]报告期末)?现金流量表|所有者权益变动表|股东权益变动表)|资产负债表|利润表|现金流量表)"
    r"(?:[（(]未经审计[）)])?(?:[（(]续[）)])?$"
)


def is_metadata_context_line(line: str) -> bool:
    """Ignore short, blank, and dot-only lines in the metadata lookahead."""
    text = line.strip()
    return len(text) >= 4 and bool(text.strip("."))


def statement_title(text: str, following_lines: list[str] | tuple[str, ...] = ()) -> str | None:
    """Require a standalone heading with statement metadata in the next five lines."""
    lines = text.strip().splitlines()
    if len(lines) == 0:
        return None;
    if len(lines) > 1:
        return None
    if "合并及公司利润表" in text:
        print("found 合并及公司利润表")

    heading = re.sub(r"[\s*#]", "", text)
    match = HEADING.fullmatch(heading)
    if not match:
        return None
    title = match.group(1).replace("合共及公司", "合并及公司")
    metadata_lines = []
    for line in [line for line in following_lines if is_metadata_context_line(line)][:5]:
        # Metadata after another heading belongs to that statement.
        if HEADING.fullmatch(re.sub(r"[\s*#]", "", line)):
            break
        metadata_lines.append(line)
    context = heading[:match.start(1)] + re.sub(r"[\s*#]", "", "".join(metadata_lines))
    if re.search(COMPARATIVE_PERIOD, context):
        return title
    if re.search(r"(?:编制单位[：:]?.+|币种[：:（(]?人民币|(?<!编制)单位[：:（(]?(?:人民币)?[十百千万亿]*元)", context):
        return title
    # 单位均为人民币百万元
    if re.search(r"单位.{0,3}(?:人民币)?[千百万]{0,3}元", context):
        return title
    if re.search(r"人民币[千百万]{0,3}元", context):
        return title
    if re.search(r"截至.*期间", context):
        return title
    if re.search("审计", context):
        return title
    if re.search(r"\d{1,2}月\d{1,2}日", context):
        return title
    if "资产负债表" in title:
        period = r"(?<!\d)20\d{2}(?:年\d{1,2}月\d{1,2}日|-\d{1,2}-\d{1,2})(?!\d)"
    elif "利润表" in title or "现金流量表" in title:
        if re.search(PERIOD_END, context) or re.search(r"(?<!\d)20\d{2}年度", context):
            return title
        if re.search(QUARTER_PERIOD, context):
            return title
        period = r"(?<!\d)20\d{2}年\d{1,2}月(?:\d{1,2}日)?\s*[—–－\-~～至到]\s*(?:\d{1,2}月)?(?:\d{1,2}日)?"
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


def page_text_lines(page: pymupdf.Page) -> list[tuple[tuple[float, float, float, float], str]]:
    """Read text lines in the same visual order on each page."""
    lines = []
    for block in page.get_text("dict", flags=pymupdf.TEXTFLAGS_TEXT)["blocks"]:
        for line in block.get("lines", []):
            text = "".join(span["text"] for span in line["spans"])
            lines.append((line["bbox"], text))
    return sorted(lines, key=lambda item: (item[0][1], item[0][0]))


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
        next_page_lines = None
        for page_number, page in enumerate(document, 1):
            if page_number <= 3 and not result.title_reporting_date:
                result.title_reporting_date = match_title_reporting_date(page.get_text(sort=True)) or ""
            lines = next_page_lines if next_page_lines is not None else page_text_lines(page)
            next_page_lines = None
            if not result.stock_code:
                # PDF cells may store the label and value in separate text lines.
                for text in page.get_text(sort=True).splitlines():
                    result.stock_code = match_stock_code(text) or ""
                    if result.stock_code:
                        break
            boundaries = []
            for index, (bbox, text) in enumerate(lines):
                if "合并及公司资产负债表" in text:
                    print(f"found at page_number {page_number} ========")
                following_lines = [line for _, line in lines[index + 1:] if is_metadata_context_line(line)][:5]
                if len(following_lines) < 5 and page_number < len(document):
                    if next_page_lines is None:
                        next_page_lines = page_text_lines(document[page_number])
                    following_lines.extend(
                        [line for _, line in next_page_lines if is_metadata_context_line(line)][:5 - len(following_lines)])
                compact = re.sub(r"\s+", "", text)
                if any(marker in compact for marker in ("法定代表人", "公司负责人", "会计工作负责人", "会计机构负责人")):
                    boundaries.append((bbox, None))
                elif (title := statement_title(text, following_lines)) is not None:
                    boundaries.append((bbox, title))
                    if STATEMENTS.get(title) == "balance_sheet" and not result.balance_sheet_reporting_date:
                        dates = match_dates(text)
                        if dates:
                            result.balance_sheet_reporting_date = dates[0]
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
                    "cash_flow_statement": r"[期年]末现金及现金等价物余额",
                    "income_statement": "稀释每股收益",
                    "balance_sheet": r"负债[和及].{0,6}权益.{0,6}总计",
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
                if STATEMENTS[active.title] == "balance_sheet" and not result.balance_sheet_reporting_date:
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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", "-o", type=Path,
                        help="Write JSON and Markdown; preserve relative folders for multiple PDFs")
    args = parser.parse_args()
    failed = False
    with LineItemStorage() as storage:
        if not storage.database_url:
            print("DATABASE_URL is not configured", file=sys.stderr)
            return 1
        try:
            reports = storage.get_downloaded_reports()
        except Exception as error:
            print(f"Unable to load Downloaded reports: {error}", file=sys.stderr)
            return 1
        if not reports:
            print("No reports at Downloaded stage.")
            return 0
        paths = [Path(report["local_path"]).resolve() for report in reports if report["local_path"]]
        root = Path(os.path.commonpath([path.parent for path in paths])) if paths else Path.cwd()
        for report in reports:
            pdf_hash = report["pdf_hash"]
            path = report["local_path"]
            try:
                if not path:
                    raise ValueError(f"Report {pdf_hash} has no local PDF path")
                path = Path(path).resolve()
                print(f"Extracting {path}({pdf_hash})")
                result = extract_report(path)
                if result.pdf_hash != pdf_hash:
                    raise ValueError(f"PDF hash mismatch: expected {pdf_hash}, got {result.pdf_hash}")
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
                    pdf_hash, payload, markdown["balance_sheet"], markdown["income_statement"],
                    markdown["cash_flow_statement"], local_path=result.pdf_file,
                    stock_code=result.stock_code, reporting_date=result.reporting_date,
                    pdf_creation_date=result.pdf_creation_time,
                )
                missing = [name for name, text in markdown.items() if not text or not text.strip()]
                if missing:
                    raise ValueError(f"Empty extracted statements: {', '.join(missing)}")
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
