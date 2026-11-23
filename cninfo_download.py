"""Download full periodic reports from the 2016 annual report onward.

Usage: python cninfo_download.py 300750
Files: reports/cninfo/STOCK_CODE/REPORT_TITLE.pdf
Wait a random 1–5 seconds before every PDF fetch, including the first.
"""

import argparse
from datetime import date, datetime, timezone
from hashlib import file_digest, md5, sha256
from http.client import IncompleteRead
from itertools import chain
from pathlib import Path
from urllib.parse import urlencode, urljoin
from urllib.request import Request, urlopen
import json
import random
import re
import sys
import time

import pymupdf

from pdf_download import download_pdf, parse_delay, report_filename, save_download_log, validate_delay_range
from line_item_storage import LineItemStorage
from report_metadata import match_filename_reporting_date


DEFAULT_REPORTS_DIR = Path(__file__).resolve().parent / "reports" / "cninfo"
SKIPPED_TITLE_MARKERS = ("已取消", "正文", "摘要")


def should_skip_title(title):
    return any(marker in title for marker in SKIPPED_TITLE_MARKERS)


def extract_local_report_metadata(local_path):
    """Extract identifiers from the stock-code directory and report filename."""
    path = Path(local_path).resolve()
    stock_code = next((part for part in reversed(path.parts[:-1])
                       if re.fullmatch(r"\d{6}", part)), None)
    reporting_date = match_filename_reporting_date(path.stem)
    if reporting_date is None:
        match = re.search(r"(?<!\d)(20\d{2})[-_.](0[1-9]|1[0-2])[-_.](0[1-9]|[12]\d|3[01])(?!\d)", path.stem)
        reporting_date = "-".join(match.groups()) if match else None
    return stock_code, reporting_date


def extract_pdf_creation_time(local_path):
    try:
        with pymupdf.open(local_path) as document:
            return (document.metadata or {}).get("creationDate") or None
    except (OSError, RuntimeError, ValueError):
        return None


def metadata_warnings(stock_code, reporting_date, pdf_creation_time):
    warnings = []
    if not stock_code:
        warnings.append("stock_code unavailable from local file path")
    if not reporting_date:
        warnings.append("reporting_date unavailable from local file name")
    if not pdf_creation_time:
        if reporting_date:
            warnings.append("pdf_creation_time unavailable from PDF metadata; using reporting_date as fallback")
        else:
            warnings.append("pdf_creation_time unavailable from PDF metadata")
    return warnings


def post_json(path, payload, timeout=30):
    request = Request(
        "https://www.cninfo.com.cn" + path,
        data=urlencode(payload).encode(),
        headers={
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://www.cninfo.com.cn/",
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    with urlopen(request, timeout=timeout) as response:
        return json.load(response)


def find_org_id(stock_code):
    matches = post_json("/new/information/topSearch/query", {
        "keyWord": stock_code, "maxNum": 20,
    })
    if not isinstance(matches, list):
        raise RuntimeError("Unexpected response from CNInfo stock lookup")
    for stock in matches:
        if stock.get("code") == stock_code and stock.get("orgId"):
            return stock["orgId"]
    raise ValueError(f"Stock code not found on CNInfo: {stock_code}")


def cninfo_pdf_urls(stock_code, org_id=None, *, column=None):
    if not re.fullmatch(r"\d{6}", stock_code):
        raise ValueError("Stock code must contain exactly six digits")
    org_id = org_id or find_org_id(stock_code)
    column = column or ("sse" if stock_code.startswith("6") else "szse")
    page = 1
    seen = set()

    while True:
        payload = {
            "stock": f"{stock_code},{org_id}",
            "column": column,
            "tabName": "fulltext",
            "category": (
                "category_ndbg_szsh;category_bndbg_szsh;"
                "category_yjdbg_szsh;category_sjdbg_szsh;"
            ),
            "pageNum": page,
            "pageSize": 30,
            "isHLtitle": "false",
            "seDate": f"2016-01-01~{date.today().isoformat()}",
            "sortName": "time",
            "sortType": "desc",
        }
        result = post_json("/new/hisAnnouncement/query", payload)
        if not isinstance(result, dict) or "announcements" not in result:
            raise RuntimeError(f"Unexpected response from CNInfo report query (page {page})")

        announcements = result.get("announcements") or []
        for item in announcements:
            title = item.get("announcementTitle") or ""
            path = item.get("adjunctUrl") or ""
            if (not path.lower().endswith(".pdf") or should_skip_title(title)
                    or "摘要" in title):
                continue
            compact_title = re.sub(r"\s+", "", title)
            if "英文" in compact_title:
                continue
            year_match = re.search(r"(?:19|20)\d{2}", compact_title)
            if not year_match:
                continue
            year = int(year_match.group())
            if year < 2016 or (year == 2016 and not re.search(r"(?<!半)年度报告|(?<!半)年报", compact_title)):
                continue
            url = urljoin("https://static.cninfo.com.cn/", path)
            if url not in seen:
                seen.add(url)
                yield {"title": title, "download_url": url}

        if not announcements or page * 30 >= int(result["totalAnnouncement"]):
            break
        page += 1
        time.sleep(1)


def completed_downloads(stock_code, output_dir):
    """Trust a completed local listing without contacting CNInfo."""
    log_path = Path(output_dir) / stock_code / "download_reports.json"
    if not log_path.is_file():
        return None
    log = json.loads(log_path.read_text(encoding="utf-8"))
    if not isinstance(log, dict) or log.get("stock_code") != stock_code:
        return None
    entries = log.get("downloads")
    # Older logs have no listing_complete flag; honor their recorded DONE state.
    if log.get("listing_complete") is False or not isinstance(entries, dict) or not entries:
        return None
    for entry in entries.values():
        if not isinstance(entry, dict) or entry.get("status") != "DONE" or not entry.get("pdf_hash"):
            return None
        filename = entry.get("file")
        if (not isinstance(filename, str) or Path(filename).name != filename
                or any(char in filename for char in ("/", "\\", "\x00"))
                or not filename.endswith(".pdf") or not (log_path.parent / filename).is_file()):
            return None
    return list(entries.values())


def download_reports(stock_code, reports, output_dir=DEFAULT_REPORTS_DIR, *, delay=(1, 5)):
    """Persist URL-based state, retry failures, and skip completed local files."""
    if not re.fullmatch(r"\d{6}", stock_code):
        raise ValueError("Stock code must contain exactly six digits")
    validate_delay_range(*delay)
    directory = Path(output_dir) / stock_code
    directory.mkdir(parents=True, exist_ok=True)
    log_path = directory / "download_reports.json"
    log = json.loads(log_path.read_text(encoding="utf-8")) if log_path.exists() else {
        "stock_code": stock_code, "downloads": {},
    }
    if not isinstance(log, dict) or log.get("stock_code") != stock_code or not isinstance(log.get("downloads"), dict):
        raise ValueError(f"Invalid download log: {log_path}")
    entries = log["downloads"]
    owners = {}
    for url, entry in entries.items():
        filename = entry.get("file") if isinstance(entry, dict) else None
        if (not isinstance(filename, str) or Path(filename).name != filename
                or any(char in filename for char in ("/", "\\", "\x00")) or not filename.endswith(".pdf")):
            raise ValueError(f"Invalid filename in download log: {log_path}")
        if filename.casefold() in owners:
            raise ValueError(f"Conflicting filenames in download log: {log_path}")
        owners[filename.casefold()] = url
    retry_reports = [{"title": entry["title"], "download_url": url}
                     for url, entry in entries.items()
                     if not should_skip_title(entry.get("title", ""))
                     if entry.get("status") in {"ERROR", "DOWNLOADING", "DOWNLOADED"}
                     or not entry.get("pdf_hash")]
    log["listing_complete"] = False
    save_download_log(log_path, log)
    seen = set()
    failed = False
    downloaded_count = 0
    skipped_count = 0
    failed_count = 0
    for report in chain(retry_reports, reports):
        title, url = report["title"], report["download_url"]
        if should_skip_title(title):
            continue
        if url in seen:
            continue
        seen.add(url)
        print(f"{title}\t{url}", flush=True)
        entry = entries.get(url)
        if entry is None:
            filename = report_filename(title)
            if filename.casefold() in owners and owners[filename.casefold()] != url:
                filename = f"{filename[:-4]}-{sha256(url.encode()).hexdigest()[:12]}.pdf"
            owners[filename.casefold()] = url
            entry = entries[url] = {"title": title, "download_url": url, "file": filename, "attempts": 0}
        destination = directory / entry["file"]
        local_stock_code, reporting_date = extract_local_report_metadata(destination)
        entry.update(stock_code=local_stock_code, reporting_date=reporting_date)
        needs_fetch = (not destination.is_file() or entry.get("status") == "DOWNLOADING"
                       or (entry.get("status") == "ERROR" and entry.get("error_stage") != "enrollment"))
        if not needs_fetch:
            skipped_count += 1
            print(f"Network download request: NO\t{url}", flush=True)
            print(f"Skipping existing file: {destination}", file=sys.stderr, flush=True)
        else:
            seconds = random.uniform(*delay)
            print(f"Waiting {seconds:.1f}s before downloading: {destination}", file=sys.stderr, flush=True)
            entry.update(status="DOWNLOADING", attempts=entry.get("attempts", 0) + 1,
                         started_at=datetime.now(timezone.utc).isoformat())
            entry.pop("pdf_hash", None)
            save_download_log(log_path, log)
            time.sleep(seconds)
            try:
                print(f"Network download request: YES\t{url}", flush=True)
                info = download_pdf(url, destination, referer="https://www.cninfo.com.cn/")
                entry.update(info or {})
            except (OSError, ValueError, IncompleteRead) as error:
                failed = True
                failed_count += 1
                entry.update(status="ERROR", error_stage="download", error=str(error),
                             updated_at=datetime.now(timezone.utc).isoformat())
                save_download_log(log_path, log)
                print(f"Failed: {url}: {error}", file=sys.stderr, flush=True)
                continue
        pdf_creation_time = extract_pdf_creation_time(destination)
        warnings = metadata_warnings(local_stock_code, reporting_date, pdf_creation_time)
        effective_pdf_creation_time = pdf_creation_time or reporting_date
        entry["pdf_creation_time"] = effective_pdf_creation_time
        if warnings:
            entry["warnings"] = warnings
            for warning in warnings:
                print(f"Warning: {destination}: {warning}", flush=True)
        else:
            entry.pop("warnings", None)
        entry["status"] = "DOWNLOADED"
        save_download_log(log_path, log)
        try:
            with destination.open("rb") as pdf:
                entry["pdf_hash"] = file_digest(pdf, lambda: md5(usedforsecurity=False)).hexdigest()
            entry["local_path"] = str(destination.resolve())
            save_download_log(log_path, log)
            with LineItemStorage() as storage:
                if not storage.database_url:
                    raise RuntimeError("DATABASE_URL is not configured")
                storage.store_downloaded_report(
                    entry["pdf_hash"], url, entry["local_path"],
                    stock_code=local_stock_code, reporting_date=reporting_date,
                    pdf_creation_date=effective_pdf_creation_time,
                    download_report=json.dumps(entry, ensure_ascii=False, sort_keys=True),
                )
                if not local_stock_code or not reporting_date:
                    storage.update_report_digestion_stage(entry["pdf_hash"], "FileErr")
            entry["status"] = "DONE"
            entry.pop("error", None)
            entry.pop("error_stage", None)
            if needs_fetch:
                downloaded_count += 1
        except Exception as error:
            failed = True
            failed_count += 1
            entry.update(status="ERROR", error_stage="enrollment", error=str(error))
            print(f"Unable to enroll {destination}: {error}", file=sys.stderr, flush=True)
        entry["updated_at"] = datetime.now(timezone.utc).isoformat()
        save_download_log(log_path, log)
    log["listing_complete"] = True
    log["listing_since"] = "2016-12-31"
    save_download_log(log_path, log)
    print(
        f"Summary: {downloaded_count} downloaded, "
        f"{skipped_count} skipped, {failed_count} failed",
        flush=True,
    )
    return int(failed)


def enroll_local_reports(stock_code, output_dir=DEFAULT_REPORTS_DIR):
    """Enroll PDFs already listed locally, without contacting CNInfo or fetching URLs."""
    directory = Path(output_dir) / stock_code
    log_path = directory / "download_reports.json"
    if not log_path.is_file():
        raise ValueError(f"Download report log not found: {log_path}")
    log = json.loads(log_path.read_text(encoding="utf-8"))
    if (not isinstance(log, dict) or log.get("stock_code") != stock_code
            or not isinstance(log.get("downloads"), dict)):
        raise ValueError(f"Invalid download report log: {log_path}")

    failed = False
    enrolled_count = 0
    failed_count = 0
    with LineItemStorage() as storage:
        if not storage.database_url:
            raise RuntimeError("DATABASE_URL is not configured")
        for url, entry in log["downloads"].items():
            if not isinstance(entry, dict):
                failed = True
                failed_count += 1
                print(f"Unable to enroll {url}: invalid download report entry", file=sys.stderr, flush=True)
                continue
            filename = entry.get("file")
            if (not isinstance(filename, str) or Path(filename).name != filename
                    or any(char in filename for char in ("/", "\\", "\x00"))
                    or not filename.endswith(".pdf")):
                failed = True
                failed_count += 1
                print(f"Unable to enroll {url}: invalid PDF filename", file=sys.stderr, flush=True)
                continue
            destination = directory / filename
            if not destination.is_file():
                failed = True
                failed_count += 1
                print(f"Unable to enroll {destination}: file not found", file=sys.stderr, flush=True)
                continue
            local_stock_code, reporting_date = extract_local_report_metadata(destination)
            pdf_creation_time = extract_pdf_creation_time(destination)
            warnings = metadata_warnings(local_stock_code, reporting_date, pdf_creation_time)
            effective_pdf_creation_time = pdf_creation_time or reporting_date
            entry.update(
                stock_code=local_stock_code, reporting_date=reporting_date,
                pdf_creation_time=effective_pdf_creation_time, local_path=str(destination.resolve()),
            )
            if warnings:
                entry["warnings"] = warnings
                for warning in warnings:
                    print(f"Warning: {destination}: {warning}", flush=True)
            else:
                entry.pop("warnings", None)
            try:
                with destination.open("rb") as pdf:
                    entry["pdf_hash"] = file_digest(pdf, lambda: md5(usedforsecurity=False)).hexdigest()
                storage.store_downloaded_report(
                    entry["pdf_hash"], entry.get("download_url", url), entry["local_path"],
                    stock_code=local_stock_code, reporting_date=reporting_date,
                    pdf_creation_date=effective_pdf_creation_time,
                    download_report=json.dumps(entry, ensure_ascii=False, sort_keys=True),
                )
                if not local_stock_code or not reporting_date:
                    storage.update_report_digestion_stage(entry["pdf_hash"], "FileErr")
                entry["status"] = "DONE"
                entry.pop("error", None)
                entry.pop("error_stage", None)
                enrolled_count += 1
                print(f"Enrolled local PDF: {destination}", flush=True)
            except Exception as error:
                failed = True
                failed_count += 1
                entry.update(status="ERROR", error_stage="enrollment", error=str(error))
                print(f"Unable to enroll {destination}: {error}", file=sys.stderr, flush=True)
            entry["updated_at"] = datetime.now(timezone.utc).isoformat()
            save_download_log(log_path, log)
    print(f"Summary: {enrolled_count} enrolled, {failed_count} failed", flush=True)
    return int(failed)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stock_code", help="Six-digit stock code, e.g. 300750")
    parser.add_argument("--org-id", help="Override CNInfo's automatically resolved organization ID")
    parser.add_argument("--column", help="Override CNInfo's market column (default: infer Shanghai/Shenzhen)")
    parser.add_argument("--delay", type=parse_delay, default=(1, 5), metavar="MIN,MAX",
                        help="Random seconds before each PDF fetch (default: 1,5)")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_REPORTS_DIR,
                        help="Download root; a stock-code subfolder is created")
    parser.add_argument("--urls-only", action="store_true", help="Print titles and URLs without downloading")
    parser.add_argument("--refresh", action="store_true", help="Query CNInfo even when the download log is complete")
    parser.add_argument("--enroll-only", action="store_true",
                        help="Enroll PDFs from the local download report without network access")
    args = parser.parse_args(argv)
    try:
        if not re.fullmatch(r"\d{6}", args.stock_code):
            raise ValueError("Stock code must contain exactly six digits")
        if args.enroll_only:
            return enroll_local_reports(args.stock_code, args.output_dir)
        cached = None if args.refresh else completed_downloads(args.stock_code, args.output_dir)
        if cached is not None:
            if args.urls_only:
                for report in cached:
                    print(f"{report['title']}\t{report['download_url']}", flush=True)
                print(f"Summary: 0 downloaded, {len(cached)} skipped, 0 failed", flush=True)
            else:
                print(f"All logged reports are downloaded for {args.stock_code}; skipping CNInfo requests.", file=sys.stderr)
                print(f"Summary: 0 downloaded, {len(cached)} skipped, 0 failed", flush=True)
            return 0
        reports = cninfo_pdf_urls(args.stock_code, args.org_id, column=args.column)
        if args.urls_only:
            count = 0
            for report in reports:
                print(f"{report['title']}\t{report['download_url']}", flush=True)
                count += 1
            print(f"Summary: {count} URLs, 0 failed", flush=True)
        else:
            return download_reports(args.stock_code, reports, args.output_dir, delay=args.delay)
    except (OSError, ValueError, RuntimeError) as error:
        print(f"Unable to process CNInfo PDFs: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
