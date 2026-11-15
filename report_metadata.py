"""Shared, side-effect-free report identifier detection."""

import re


def extract_pdf_metadata(text: str) -> dict[str, str]:
    """Read source PDF metadata from the Markdown's hidden comments."""
    fields = {
        "pdf_hash": "ORIGINAL_PDF_FILE_HASH",
        "pdf_creation_time": "ORIGINAL_PDF_FILE_CREATION_DATE",
        "pdf_local_path": "ORIGINAL_PDF_FILE",
        "pdf_download_url": "ORIGINAL_PDF_FILE_DOWNLOAD_URL",
    }
    metadata = {}
    for field, marker in fields.items():
        match = re.search(rf"(?m)^[ \t]*<!--[ \t]*{marker}:[ \t]*([^\r\n]*?)[ \t]*-->[ \t]*$", text)
        metadata[field] = match.group(1).strip() if match else ""
    return metadata


def match_stock_code(line: str) -> str | None:
    match = re.search(r"(?:证券|公司|股票)代码\s*[：:|]?\s*(\d{6})", line.replace("*", ""))
    return match.group(1) if match else None


def is_balance_sheet_title(line: str) -> bool:
    title = re.sub(r"[ \*0-9,、]", "", line)
    return "合并资产负债表" in title and len(title) < len("合并资产负债表") + 3


def match_dates(line: str) -> list[str]:
    return [
        f"{year}-{month.zfill(2)}-{day.zfill(2)}"
        for year, month, day in re.findall(r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日", line)
    ]


def extract_report_identifiers(text: str) -> tuple[str, str]:
    """Use the digest's first-stock-code and balance-sheet-date rules."""
    stock_code = ""
    reporting_date = ""
    in_balance_sheet = False
    for line in text.splitlines():
        if not stock_code:
            stock_code = match_stock_code(line) or ""
            if stock_code:
                continue
        if not in_balance_sheet:
            in_balance_sheet = is_balance_sheet_title(line)
        elif not reporting_date:
            dates = match_dates(line)
            if dates and dates.count(dates[0]) == 1:
                reporting_date = dates[0]
        if stock_code and reporting_date:
            break
    return stock_code, reporting_date
