"""Extract identifiers and consolidated financial statements from text PDFs.

Usage: python extract.py --output-dir extracted/pdf
Processes registered reports at the FileOK stage using their local_path.
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
from line_item_chart import LineItemChart
from line_item_storage import LineItemStorage
from report_sources import normalized_bbox, warm_statement_previews
from statement_digestion import normalize_label


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
STATEMENT_LOOKAHEAD_LINES = 28
PERIOD_END = r"截至20\d{2}年\d{1,2}月\d{1,2}日止(?:\d{1,2}|[一二三四五六七八九十两]{1,3})个月期间"
QUARTER_PERIOD = r"20\d{2}年第?[一二三四1-4]季度"
DATE_RANGE_PERIOD = r"20\d{2}年\d{1,2}月\d{1,2}日[至到](?:20\d{2}年)?\d{1,2}月\d{1,2}日止期间"
MONTH_RANGE_PERIOD = r"20\d{2}年\d{1,2}(?:月)?[—–－\-~～至到]\d{1,2}月"
COMPARATIVE_PERIOD = (
    r"(?:截至20\d{2}年\d{1,2}月\d{1,2}日[和及]20\d{2}年\d{1,2}月\d{1,2}日|"
    r"20\d{2}年\d{1,2}[—–－\-~～至到]\d{1,2}月[和及]20\d{2}年\d{1,2}[—–－\-~～至到]\d{1,2}月)的"
)
HEADING = re.compile(
    r"^(?:[\d一二三四五六七八九十]+(?:-[\d一二三四五六七八九十]+)*[、.．]|"
    r"[（(][\d一二三四五六七八九十]+[）)])?"
    rf"(?:{COMPARATIVE_PERIOD}|{PERIOD_END}|{DATE_RANGE_PERIOD}|20\d{{2}}年\d{{1,2}}月\d{{1,2}}日|20\d{{2}}年度|{QUARTER_PERIOD})?"
    r"(?:未经审计)?"
    r"((?:(?:中期)?合并(?:及(?:母公司|公司))?|合共及公司|母公司)(?:资产负债表|(?:年初[到至]报告期末)?利润表|"
    r"(?:年初[到至]报告期末)?现金流量表|所有者权益变动表|股东权益变动表)|资产负债表|利润表|现金流量表)"
    r"(?:[（(]未经审计[）)])?(?:[（(]续[）)])?$"
)


def is_metadata_context_line(line: str) -> bool:
    """Ignore short, blank, and dot-only lines in the metadata lookahead."""
    text = line.strip()
    return len(text) >= 4 and bool(text.strip("."))


def _statement_item_names(title: str) -> list[str]:
    if "资产负债表" in title:
        prefixes = range(11, 32)
    elif "利润表" in title:
        prefixes = range(41, 60)
    elif "现金流量表" in title:
        prefixes = range(81, 88)
    else:
        return []
    return [name for name, item in LineItemChart().items.items()
            if int(str(item.item_id)[:2]) in prefixes]


def normalize_item_name(label):
    label = re.sub(r"^(?:其中|加|减)[：:]", "", label)
    label = re.sub(r"（减少）", "", label)
    label = re.sub(r"（[^）]*(?:净亏损|亏损总额|亏损|损失)[^）]*）$", "", label)
    return label.rstrip("：:")

def _has_ordered_statement_rows(blocks: list[str] | tuple[str, ...], title: str) -> bool:
    names = _statement_item_names(title)
    if not names:
        return False
    name_order = {name: index for index, name in enumerate(names)}
    numeric = r"[-+−]?(?:\d[\d,]*(?:\.\d+)?|\.\d+)"
    other_block = re.compile(r"^[一二三四五六七八九十\d、()（）\[\]【】\s]+$")
    item_count = 0
    non_item_count = 0
    previous_order = -1
    for raw_block in blocks:
        block = normalize_label(raw_block.strip())
        matching_names = [name for name in names if block.startswith(name)]
        if not matching_names and len(block) > 6:
            matching_names = [name for name in names if name.startswith(block)]

        if matching_names:
            row_order = max(name_order[name] for name in matching_names)
            if row_order < previous_order:
                continue
            item_count += 1
            non_item_count = 0
            previous_order = row_order
            if item_count >= 5:
                return True
            continue

        if not block or re.fullmatch(numeric, block) or other_block.fullmatch(block):
            if item_count:
                non_item_count += 1
                if non_item_count <= 6:
                    continue

        item_count = 0
        non_item_count = 0
        previous_order = -1
    return False


def statement_title(text: str, following_lines: list[str] | tuple[str, ...] = ()) -> str | None:
    """Require a standalone heading with metadata and ordered statement rows."""
    lines = text.strip().splitlines()
    if len(lines) == 0:
        return None;
    if len(lines) > 1:
        return None

    if "合并现金流量表" in text:
        print("found 合并现金流量表")

    heading = re.sub(r"[\s*#]", "", text)
    match = HEADING.fullmatch(heading)
    if not match:
        return None
    title = match.group(1)
    if not any(marker in title for marker in ("资产负债表", "利润表", "现金流量表")):
        return None
    if not _has_ordered_statement_rows(following_lines, title):
        return None
    context_lines = [line for line in following_lines if is_metadata_context_line(line)][:5]
    metadata_lines = []
    for line in context_lines:
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
    if re.search(r"\d{1,2}月\d{1,2}日", context) and len(context) < 16:
        return title
    if "资产负债表" in title:
        period = r"(?<!\d)20\d{2}(?:年\d{1,2}月\d{1,2}日|-\d{1,2}-\d{1,2})(?!\d)"
    elif "利润表" in title or "现金流量表" in title:
        if (re.search(PERIOD_END, context) or re.search(r"(?<!\d)20\d{2}年度", context)
                or re.search(MONTH_RANGE_PERIOD, context)):
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
    source_rows: list[dict] = field(default_factory=list)

    def markdown(self) -> str:
        """Include the caption (notably currency units) and extracted tables."""
        # PDF text extraction can concatenate the caption and the whole table
        # into one line. Keep only caption values, never that complete line.
        captions = match_dates(self.text)
        captions.extend(re.findall(
            r"(?<!编制)(?:金额)?单位\s*[:：]?\s*(?:均为|为)?\s*"
            r"(?:人民币\s*)?(?:百万元|亿元|万元|千元|元)",
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
    balance_sheet: Statement | None = None
    income_statement: Statement | None = None
    cash_flow_statement: Statement | None = None
    title_reporting_date: str = ""
    balance_sheet_reporting_date: str = ""
    reporting_dates_match: bool | None = None
    warnings: list[str] = field(default_factory=list)


def page_text_lines(page: pymupdf.Page) -> list[tuple[tuple[float, float, float, float], str]]:
    """Read text lines in the same visual order on each page."""
    lines = []
    for block in page.get_text("dict", flags=pymupdf.TEXTFLAGS_TEXT)["blocks"]:
        for line in block.get("lines", []):
            text = "".join(span["text"] for span in line["spans"])
            lines.append((line["bbox"], text))
    return sorted(lines, key=lambda item: (item[0][1], item[0][0]))


def _read_pdf_hash(path: Path) -> str:
    with path.open("rb") as source:
        return hashlib.file_digest(source, "md5").hexdigest()


def _find_page_boundaries(document, page_number: int, lines, result: ReportExtraction,
                          next_page_lines):
    boundaries = []
    statement_end_patterns = {
        "balance_sheet": re.compile(r"负债.{0,6}权益.{0,8}总计"),
        "income_statement": re.compile(r"(?:基本|稀释)?每股收益"),
        "cash_flow_statement": re.compile(r"期末现金及现金等价物余额"),
    }
    current_statement = next(
        (name for name in ("cash_flow_statement", "income_statement", "balance_sheet")
         if getattr(result, name) is not None),
        None,
    )
    previous_statement_text = (
        getattr(result, current_statement).text if current_statement else ""
    )
    statement_end_seen = bool(
        current_statement
        and statement_end_patterns[current_statement].search(
            re.sub(r"\s+", "", previous_statement_text)
        )
    )
    for index, (bbox, text) in enumerate(lines):
        following_lines = [line for _, line in lines[index + 1:]
                           if is_metadata_context_line(line)][:STATEMENT_LOOKAHEAD_LINES]
        if len(following_lines) < STATEMENT_LOOKAHEAD_LINES and page_number < len(document):
            if next_page_lines is None:
                next_page_lines = page_text_lines(document[page_number])
            following_lines.extend(
                [line for _, line in next_page_lines if is_metadata_context_line(line)
                 ][:STATEMENT_LOOKAHEAD_LINES - len(following_lines)])
        compact = re.sub(r"\s+", "", text)
        title = None
        if current_statement and statement_end_patterns[current_statement].search(compact):
            statement_end_seen = True
        markers_found = any(marker in compact for marker in (
            "法定代表人", "公司负责人", "会计工作负责人", "会计机构负责人"))
        if markers_found and current_statement and statement_end_seen:
            boundaries.append((bbox, None))
        else:
            title = statement_title(text, following_lines)
        if title is not None:
            boundaries.append((bbox, title))
            current_statement = STATEMENTS.get(title)
            statement_end_seen = False
            if STATEMENTS.get(title) == "balance_sheet" and not result.balance_sheet_reporting_date:
                dates = match_dates(text)
                if dates:
                    result.balance_sheet_reporting_date = dates[0]
    return boundaries, next_page_lines


def _statement_ending(marker: str | None, lines, top: float, bottom: float, table_data):
    if not marker:
        return None
    ending = min((bbox[3] for bbox, line in lines
                  if top <= bbox[1] and bbox[3] <= bottom
                  and re.search(marker, re.sub(r"\s+", "", line))), default=None)
    for table, extracted in table_data:
        for row, cells in zip(table.rows, extracted):
            if re.search(marker, re.sub(r"\s+", "", "".join(cell or "" for cell in cells))):
                if ending is None or row.bbox[1] <= ending <= row.bbox[3]:
                    ending = row.bbox[3]
    return ending


def _collect_statement_region(page, page_number: int, lines, active: Statement | None,
                              result: ReportExtraction, top: float, bottom: float) -> Statement | None:
    if active is None or bottom <= top:
        return active
    clip = pymupdf.Rect(0, top, page.rect.width, bottom)
    with redirect_stdout(sys.stderr):
        tables = page.find_tables(clip=clip, strategy="lines_strict")
        table_data = [(table, table.extract()) for table in tables.tables]
    marker = {
        "cash_flow_statement": r"[期年]末现金及现金等价物余额",
        "income_statement": "稀释每股收益",
        "balance_sheet": r"负债[和及].{0,6}权益.{0,6}总计",
    }.get(STATEMENTS[active.title])
    ending = _statement_ending(marker, lines, top, bottom, table_data)
    if ending is not None:
        clip.y1 = ending
    text = page.get_text(clip=clip, sort=True).strip()
    if not text:
        return active
    if page_number not in active.pages:
        active.pages.append(page_number)
    active.text += text + "\n"
    if STATEMENTS[active.title] == "balance_sheet" and not result.balance_sheet_reporting_date:
        dates = match_dates(text)
        if dates:
            result.balance_sheet_reporting_date = dates[0]
    for table, extracted in table_data:
        for source_row, cells in zip(table.rows, extracted):
            if ending is None or source_row.bbox[1] < ending:
                active.source_rows.append({"page_number": page_number,
                    "bbox": normalized_bbox(page, source_row.bbox),
                    "cells": [cell or "" for cell in cells]})
        retained = [cells for row, cells in zip(table.rows, extracted)
                    if ending is None or row.bbox[1] < ending]
        active.rows.extend(clean_rows(retained))
    return None if ending is not None else active


def _start_statement(result: ReportExtraction, active: Statement | None, title: str | None) -> Statement | None:
    if title not in STATEMENTS:
        return None
    name = STATEMENTS[title]
    existing = getattr(result, name)
    if existing is None:
        statement = Statement(title)
        setattr(result, name, statement)
        return statement
    if active is existing and existing.title == title:
        return active
    return None


def extract_report(pdf_path: str | Path) -> ReportExtraction:
    """Extract the first set of consolidated statements, including continuations."""
    path = Path(pdf_path).resolve()
    result = ReportExtraction(pdf_file=str(path), pdf_hash=_read_pdf_hash(path))
    active: Statement | None = None
    with pymupdf.open(path) as document:
        next_page_lines = None
        for page_number, page in enumerate(document, 1):
            if page_number <= 3 and not result.title_reporting_date:
                result.title_reporting_date = match_title_reporting_date(page.get_text(sort=True)) or ""
            lines = next_page_lines if next_page_lines is not None else page_text_lines(page)
            next_page_lines = None
            if not result.stock_code:
                for text in page.get_text(sort=True).splitlines():
                    result.stock_code = match_stock_code(text) or ""
                    if result.stock_code:
                        break
            boundaries, next_page_lines = _find_page_boundaries(
                document, page_number, lines, result, next_page_lines)
            top = 0.0
            for bbox, title in boundaries:
                active = _collect_statement_region(page, page_number, lines, active, result, top, bbox[1])
                active = _start_statement(result, active, title)
                top = bbox[3]
            active = _collect_statement_region(page, page_number, lines, active, result, top, page.rect.height)
    result.reporting_date = result.balance_sheet_reporting_date or result.title_reporting_date
    if result.balance_sheet_reporting_date and result.title_reporting_date:
        result.reporting_dates_match = result.balance_sheet_reporting_date == result.title_reporting_date
    if not result.stock_code:
        for component in reversed(path.parts):
            code = re.search(r"(?<!\d)((?:00|60|30|68)\d{4})(?!\d)", component)
            if code:
                result.stock_code = code.group(1)
                break
    return result


def compare_metadata(registered: dict, extracted: ReportExtraction) -> list[str]:
    """Return metadata conflicts, using a registered date if the PDF omits it."""
    mismatches = []
    for field in ("stock_code", "reporting_date"):
        registered_value = registered.get(field)
        if not registered_value:
            continue
        registered_text = registered_value.isoformat() if hasattr(registered_value, "isoformat") else str(registered_value)
        extracted_text = getattr(extracted, field)
        if field == "reporting_date" and not extracted_text:
            setattr(extracted, field, registered_text)
            warning = f"reporting_date unavailable in PDF; using database value {registered_text}"
            extracted.warnings.append(warning)
            continue
        if registered_text != extracted_text:
            mismatches.append(f"{field}: database={registered_text!r}, PDF={extracted_text!r}")
    return mismatches


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", "-o", type=Path,
                        help="Write JSON and Markdown; preserve relative folders for multiple PDFs")
    parser.add_argument("--hash", dest="pdf_hash",
                        help="Extract one registered PDF by its hash, regardless of current stage")
    parser.add_argument("--stock_code", help="Extract only reports for this stock code")
    return parser.parse_args()


def _load_reports(storage: LineItemStorage, pdf_hash: str | None,
                  stock_code: str | None) -> list[dict]:
    if pdf_hash:
        report = storage.get_report_by_hash(pdf_hash)
        return [report] if report and (not stock_code or report.get("stock_code") == stock_code) else []
    return (storage.get_reports_at_FileOK(stock_code=stock_code)
            if stock_code else storage.get_reports_at_FileOK())


def _reports_root(reports: list[dict]) -> Path:
    paths = [Path(report["local_path"]).resolve() for report in reports if report["local_path"]]
    return Path(os.path.commonpath([path.parent for path in paths])) if paths else Path.cwd()


def _write_extraction_outputs(output_dir: Path, path: Path, root: Path,
                              payload: str, markdown: dict[str, str | None]) -> Path:
    destination = output_dir / path.relative_to(root).with_suffix("")
    destination.mkdir(parents=True, exist_ok=True)
    (destination / "extraction_report.json").write_text(payload + "\n", encoding="utf-8")
    for name in STATEMENT_FIELDS:
        if markdown[name] is not None:
            (destination / f"{name}.md").write_text(markdown[name], encoding="utf-8")
    return destination


def _print_extraction_summary(result: ReportExtraction) -> None:
    for label, name in zip(("BS", "IS", "CF"), STATEMENT_FIELDS):
        statement = getattr(result, name)
        if statement is None:
            print(f"\t{label} not extracted")
        else:
            print(f"\t{label} {statement.title}\t@ {statement.pages}")


def _extract_one_report(storage: LineItemStorage, report: dict, output_dir: Path | None, root: Path) -> None:
    pdf_hash = report["pdf_hash"]
    path_value = report["local_path"]
    if not path_value:
        raise ValueError(f"Report {pdf_hash} has no local PDF path")
    path = Path(path_value).resolve()
    print(f"Extracting {path}({pdf_hash})")
    result = extract_report(path)
    if result.pdf_hash != pdf_hash:
        raise ValueError(f"PDF hash mismatch: expected {pdf_hash}, got {result.pdf_hash}")
    mismatches = compare_metadata(report, result)
    for warning in result.warnings:
        print(f"Warning: {path}: {warning}", flush=True)
    if mismatches:
        raise ValueError(f"PDF metadata mismatch: {'; '.join(mismatches)}")
    payload = json.dumps(asdict(result), ensure_ascii=False, indent=2)
    markdown = {
        name: statement.markdown() if (statement := getattr(result, name)) is not None else None
        for name in STATEMENT_FIELDS
    }
    destination = _write_extraction_outputs(output_dir, path, root, payload, markdown) if output_dir else None
    storage.store_extracted_report(
        pdf_hash, payload, markdown["balance_sheet"], markdown["income_statement"],
        markdown["cash_flow_statement"], local_path=result.pdf_file,
        stock_code=result.stock_code, reporting_date=result.reporting_date,
    )
    warm_statement_previews(result)
    missing = [name for name, text in markdown.items() if not text or not text.strip()]
    if missing:
        raise ValueError(f"Empty extracted statements: {', '.join(missing)}")
    if destination:
        print(destination)
    _print_extraction_summary(result)


def _mark_extraction_error(storage: LineItemStorage, pdf_hash: str, path: object) -> None:
    # A failed database write must not poison the next report's transaction.
    storage.close()
    if not storage.database_url:
        return
    try:
        storage.update_report_digestion_stage(pdf_hash, "ExtractErr")
    except Exception as stage_error:
        print(f"{path}: unable to save ExtractErr: {stage_error}", file=sys.stderr)
        storage.close()


def main() -> int:
    args = _parse_args()
    failed = False
    succeeded = 0
    failures = 0
    with LineItemStorage() as storage:
        if not storage.database_url:
            print("DATABASE_URL is not configured", file=sys.stderr)
            return 1
        try:
            reports = _load_reports(storage, args.pdf_hash, args.stock_code)
        except Exception as error:
            print(f"Unable to load reports: {error}", file=sys.stderr)
            return 1
        if not reports:
            if args.pdf_hash:
                print(f"No report found for hash {args.pdf_hash}.", file=sys.stderr)
                return 1
            print("No reports at FileOK stage.")
            return 0
        root = _reports_root(reports)
        for report in reports:
            pdf_hash = report["pdf_hash"]
            path = report["local_path"]
            try:
                _extract_one_report(storage, report, args.output_dir, root)
                succeeded += 1
            except Exception as error:
                failed = True
                failures += 1
                print(f"{path}: {error}", file=sys.stderr)
                _mark_extraction_error(storage, pdf_hash, path)
    print(f"Summary: {succeeded} succeeded, {failures} failed", flush=True)
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
