"""Download Sina financial reports serially and record their source metadata.

Usage: python pdf_download.py 600519
English-version and H-share announcements are excluded. Pagination is not followed.
PDF requests are separated by a configurable random delay (default: 1–5 seconds).
"""

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
from html.parser import HTMLParser
from http.client import IncompleteRead
import json
import math
import os
from pathlib import Path
import random
import re
import sys
import tempfile
import time
from urllib.error import URLError
from urllib.parse import urljoin, urlsplit
from urllib.request import Request, urlopen

from dotenv import load_dotenv

from line_item_storage import LineItemStorage


# Load project configuration even when launched from another working directory.
load_dotenv(Path(__file__).resolve().with_name(".env"), override=False)


BASE_URL = "https://vip.stock.finance.sina.com.cn"
DEFAULT_REPORTS_DIR = Path(__file__).resolve().parent / "reports" / "download"
REPORT_PAGES = {
    "annual": ("vCB_Bulletin", "ndbg"),
    "half_year": ("vCB_BulletinZhong", "zqbg"),
    "first_quarter": ("vCB_BulletinYi", "yjdbg"),
    "third_quarter": ("vCB_BulletinSan", "sjdbg"),
}


class DatelistParser(HTMLParser):
    """Collect anchors inside any div with the datelist class, in source order."""

    def __init__(self, page_url: str):
        super().__init__(convert_charrefs=True)
        self.page_url = page_url
        self.links: list[dict[str, str]] = []
        self.found = False
        self.div_depth = 0
        self.href: str | None = None
        self.text: list[str] = []

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "div":
            if self.div_depth:
                self.div_depth += 1
            elif "datelist" in (attributes.get("class") or "").split():
                self.found = True
                self.div_depth = 1
        if tag == "a" and self.div_depth:
            self.href = attributes.get("href")
            self.text = []

    def handle_data(self, data):
        if self.href is not None:
            self.text.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self.href is not None:
            href = self.href.strip()
            url = urljoin(self.page_url, href)
            if href and urlsplit(url).scheme in {"http", "https"}:
                self.links.append({"title": " ".join("".join(self.text).split()), "url": url})
            self.href = None
            self.text = []
        if tag == "div" and self.div_depth:
            self.div_depth -= 1
            if not self.div_depth:
                self.href = None
                self.text = []


def parse_datelist(html: str, page_url: str) -> list[dict[str, str]]:
    parser = DatelistParser(page_url)
    parser.feed(html)
    parser.close()
    if not parser.found:
        raise ValueError(f"No div.datelist found on {page_url}")
    return parser.links


def decode_html(content: bytes, charset: str | None = None) -> str:
    """Honor HTTP/meta encodings; Sina's legacy pages declare GB2312."""
    if not charset:
        match = re.search(br"<meta\b[^>]*charset\s*=\s*[\"']?\s*([\w-]+)", content[:8192], re.I)
        charset = match.group(1).decode("ascii") if match else "gb18030"
    if charset.lower().replace("-", "") in {"gb2312", "gbk"}:
        charset = "gb18030"
    return content.decode(charset)


def fetch_html(page_url: str, timeout: float = 30) -> tuple[str, str]:
    request = Request(page_url, headers={"User-Agent": "Mozilla/5.0", "Accept": "text/html"})
    try:
        with urlopen(request, timeout=timeout) as response:
            html = decode_html(response.read(), response.headers.get_content_charset())
            return html, response.geturl()
    except (OSError, ValueError, LookupError) as error:
        raise RuntimeError(f"Failed to fetch or parse {page_url}: {error}") from error


def fetch_page_links(page_url: str, timeout: float = 30) -> list[dict[str, str]]:
    html, final_url = fetch_html(page_url, timeout)
    return parse_datelist(html, final_url)


class DownloadLinkParser(HTMLParser):
    """Match anchor text even when it contains nested tags or whitespace."""

    def __init__(self, page_url: str):
        super().__init__(convert_charrefs=True)
        self.page_url = page_url
        self.download_url: str | None = None
        self.href: str | None = None
        self.text: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self.href = dict(attrs).get("href")
            self.text = []

    def handle_data(self, data):
        if self.href is not None:
            self.text.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self.href is not None:
            if not self.download_url and re.sub(r"\s+", "", "".join(self.text)) == "下载公告":
                href = self.href.strip()
                url = urljoin(self.page_url, href)
                if href and urlsplit(url).scheme in {"http", "https"}:
                    self.download_url = url
            self.href = None
            self.text = []


def parse_download_link(html: str, page_url: str) -> str:
    parser = DownloadLinkParser(page_url)
    parser.feed(html)
    parser.close()
    if not parser.download_url:
        raise ValueError(f"No 下载公告 link found on {page_url}")
    return parser.download_url


class MissingDownloadLinkError(RuntimeError):
    """An announcement has no downloadable attachment (an expected skip)."""


def is_expected_missing_download(entry: dict) -> bool:
    # Recognize older logs as well as the structured reason on new results.
    return (entry.get("skip_reason") == "NO_DOWNLOAD_LINK"
            or "No 下载公告 link found" in entry.get("error", ""))


def fetch_download_link(page_url: str, timeout: float = 30) -> str:
    html, final_url = fetch_html(page_url, timeout)
    try:
        return parse_download_link(html, final_url)
    except ValueError as error:
        raise MissingDownloadLinkError(f"Failed to parse {page_url}: {error}") from error


def fetch_report_links(stock_code: str, timeout: float = 30) -> dict[str, list[dict[str, str]]]:
    """Return titles and absolute URLs grouped by the four report categories.

    Stock codes must be six-digit strings (preserving leading zeros). A failed
    page raises an error instead of returning an apparently complete result.
    """
    if not isinstance(stock_code, str) or not re.fullmatch(r"[0-9]{6}", stock_code):
        raise ValueError("stock_code must be a six-digit string, e.g. '600519'")
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout must be a positive finite number")
    urls = [f"{BASE_URL}/corp/go.php/{endpoint}/stockid/{stock_code}/page_type/{page_type}.phtml"
            for endpoint, page_type in REPORT_PAGES.values()]
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = [executor.submit(fetch_page_links, url, timeout) for url in urls]
        return {category: future.result() for category, future in zip(REPORT_PAGES, futures)}


def excluded_title(title: str) -> bool:
    normalized = re.sub(r"\s+", "", title).upper()
    return any(label in normalized for label in ("英文", "H股", "港股", "偿付能力"))


def fetch_report_download_links(stock_code: str, timeout: float = 30) -> dict[str, list[dict[str, str]]]:
    """Exclude English/H-share announcements; preserve categories and order.

    Each result has title, url (announcement), and download_url. Duplicate detail
    URLs are fetched only once. Unresolvable detail pages remain in the results
    with an empty download_url and an error; other announcements still proceed.
    """
    listings = fetch_report_links(stock_code, timeout)
    filtered = {category: [link for link in links if not excluded_title(link["title"])]
                for category, links in listings.items()}
    urls = dict.fromkeys(link["url"] for links in filtered.values() for link in links)
    with ThreadPoolExecutor(max_workers=4) as executor:
        pending = {url: executor.submit(fetch_download_link, url, timeout) for url in urls}
        downloads = {}
        for url, future in pending.items():
            try:
                downloads[url] = {"download_url": future.result()}
            except MissingDownloadLinkError as error:
                downloads[url] = {"download_url": "", "skip_reason": "NO_DOWNLOAD_LINK", "warning": str(error)}
                print(f"Warning: {error}", file=sys.stderr, flush=True)
            except RuntimeError as error:
                downloads[url] = {"download_url": "", "error": str(error)}
                print(f"Warning: {error}", file=sys.stderr, flush=True)
    return {category: [{**link, **downloads[link["url"]]} for link in links]
            for category, links in filtered.items()}


def download_pdf(download_url: str, destination: Path, timeout: float = 30,
                 referer: str = BASE_URL) -> dict:
    """Publish atomically after length/header/EOF checks and flushing to disk.

    Exceptions and Ctrl-C remove the temporary file. A forced kill may leave a
    hidden .part file, but never publishes it under the final PDF filename.
    """
    if urlsplit(download_url).scheme not in {"http", "https"}:
        raise ValueError(f"Unsupported download URL: {download_url}")
    request = Request(download_url, headers={"User-Agent": "Mozilla/5.0", "Referer": referer})
    temporary = None
    try:
        with urlopen(request, timeout=timeout) as response:
            with tempfile.NamedTemporaryFile(dir=destination.parent, prefix=f".{destination.name}.",
                                             suffix=".part", delete=False) as output:
                temporary = Path(output.name)
                size = 0
                header = b""
                tail = b""
                while chunk := response.read(1024 * 1024):
                    output.write(chunk)
                    size += len(chunk)
                    tail = (tail + chunk)[-4096:]
                    if len(header) < 1024:
                        header += chunk[:1024 - len(header)]
                expected = response.headers.get("Content-Length")
                if expected is not None and size != int(expected):
                    raise ValueError(f"Incomplete download: expected {expected} bytes, received {size}")
                if b"%PDF-" not in header:
                    raise ValueError("Response is not a PDF")
                if not re.search(rb"%%EOF\s*$", tail):
                    raise ValueError("Incomplete PDF: missing final %%EOF marker")
                output.flush()
                os.fsync(output.fileno())
            info = {"resolved_url": response.geturl(), "size_bytes": size}
        os.replace(temporary, destination)
        return info
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def save_download_log(path: Path, metadata: dict) -> None:
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=".downloads-", suffix=".json.tmp", delete=False) as output:
            temporary = Path(output.name)
            json.dump(metadata, output, ensure_ascii=False, indent=2)
            output.write("\n")
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def report_filename(title: str) -> str:
    """Keep readable titles while removing path separators and unsafe characters."""
    stem = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", title).strip(" .")
    if stem.lower().endswith(".pdf"):
        stem = stem[:-4]
    # File-name limits are measured in bytes on common filesystems.
    stem = stem.encode("utf-8")[:180].decode("utf-8", errors="ignore").rstrip(" .") or "report"
    if re.fullmatch(r"(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?", stem, re.I):
        stem = "_" + stem
    return stem + ".pdf"


def validate_delay_range(delay_min: float, delay_max: float) -> None:
    if not math.isfinite(delay_min) or not math.isfinite(delay_max) or not 0 <= delay_min <= delay_max:
        raise ValueError("delay range must be finite and satisfy 0 <= MIN <= MAX")


def parse_delay(value: str) -> tuple[float, float]:
    """Parse the shared --delay MIN,MAX argument before any network requests."""
    try:
        low, high = value.split(",")
        bounds = (float(low), float(high))
        validate_delay_range(*bounds)
        return bounds
    except ValueError as error:
        raise argparse.ArgumentTypeError("expected MIN,MAX with finite 0 <= MIN <= MAX, e.g. 1,5") from error


def record_download(destination: Path, download_url: str) -> str:
    """Hash the completed local PDF and register its download metadata."""
    with destination.open("rb") as pdf:
        pdf_hash = hashlib.file_digest(pdf, lambda: hashlib.md5(usedforsecurity=False)).hexdigest()
    with LineItemStorage() as storage:
        storage.store_downloaded_report(pdf_hash, download_url, str(destination.resolve()))
    return pdf_hash


def download_reports(stock_code: str, listings: dict[str, list[dict[str, str]]],
                     output_dir: Path | None = None, timeout: float = 30,
                     *, delay_min: float = 1, delay_max: float = 5) -> dict:
    """Download unique PDFs serially, preserve existing files, and log attempts.

    downloads.json is updated after each file; previous runs are retained. A
    failed file is recorded and the batch continues. Timestamps are UTC ISO 8601.
    """
    if not isinstance(stock_code, str) or not re.fullmatch(r"[0-9]{6}", stock_code):
        raise ValueError("stock_code must be a six-digit string")
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout must be a positive finite number")
    validate_delay_range(delay_min, delay_max)
    directory = (Path(output_dir) if output_dir is not None else DEFAULT_REPORTS_DIR) / stock_code
    directory.mkdir(parents=True, exist_ok=True)
    log_path = directory / "downloads.json"
    metadata = json.loads(log_path.read_text(encoding="utf-8")) if log_path.exists() else {"stock_code": stock_code, "downloads": []}
    if not isinstance(metadata, dict) or metadata.get("stock_code") != stock_code or not isinstance(metadata.get("downloads"), list):
        raise ValueError(f"Invalid existing download log: {log_path}")
    attempted = False
    seen = set()
    owners = {entry["file"].casefold(): entry["download_url"] for entry in metadata["downloads"]
              if "file" in entry and "download_url" in entry}
    for category, links in listings.items():
        for link in links:
            if excluded_title(link["title"]):
                continue
            url = link.get("download_url", "")
            if is_expected_missing_download(link):
                now = datetime.now(timezone.utc).isoformat()
                metadata["downloads"].append({
                    **link, "category": category, "status": "SKIPPED",
                    "skip_reason": "NO_DOWNLOAD_LINK", "started_at": now, "finished_at": now,
                })
                save_download_log(log_path, metadata)
                continue
            if not url or link.get("error"):
                now = datetime.now(timezone.utc).isoformat()
                metadata["downloads"].append({
                    **link, "category": category, "status": "ERROR",
                    "error": link.get("error") or "No download URL available",
                    "started_at": now, "finished_at": now,
                })
                save_download_log(log_path, metadata)
                continue
            if url in seen:
                continue
            seen.add(url)
            filename = report_filename(link["title"])
            # Add a suffix only when another URL has claimed the same title.
            if filename.casefold() in owners and owners[filename.casefold()] != url:
                filename = f"{filename[:-4]}-{hashlib.sha256(url.encode()).hexdigest()[:12]}.pdf"
            owners[filename.casefold()] = url
            destination = directory / filename
            entry = {**link, "category": category, "file": filename,
                     "started_at": datetime.now(timezone.utc).isoformat()}
            if destination.exists():
                entry["status"] = "SKIPPED"
                print(f"Skipping existing file: {destination}", file=sys.stderr, flush=True)
            else:
                if attempted:
                    delay = random.uniform(delay_min, delay_max)
                    print(f"Waiting {delay:.1f}s before next PDF", file=sys.stderr, flush=True)
                    time.sleep(delay)
                attempted = True
                entry["started_at"] = datetime.now(timezone.utc).isoformat()
                print(f"Downloading: {destination}", file=sys.stderr, flush=True)
                try:
                    entry.update(download_pdf(url, destination, timeout, referer=link["url"]))
                    entry["status"] = "DONE"
                    entry["downloaded_at"] = datetime.now(timezone.utc).isoformat()
                except (OSError, ValueError, IncompleteRead) as error:
                    entry["status"] = "ERROR"
                    entry["error"] = str(error)
                    print(f"Failed: {url}: {error}", file=sys.stderr, flush=True)
            # Also register existing files so a database failure can be retried
            # without downloading the PDF again. Never register a failed download.
            if entry["status"] in {"DONE", "SKIPPED"}:
                try:
                    entry["pdf_hash"] = record_download(destination, url)
                except Exception as error:
                    entry["status"] = "ERROR"
                    entry["error"] = f"Unable to register downloaded PDF: {error}"
                    print(f"Failed: {destination}: {entry['error']}", file=sys.stderr, flush=True)
            entry["finished_at"] = datetime.now(timezone.utc).isoformat()
            metadata["downloads"].append(entry)
            save_download_log(log_path, metadata)
    if not log_path.exists():
        save_download_log(log_path, metadata)
    return metadata


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stock_code", help="Six-digit stock code")
    parser.add_argument("--timeout", type=float, default=30, help="Per-page timeout in seconds")
    parser.add_argument("--delay", type=parse_delay, default=(1, 5), metavar="MIN,MAX",
                        help="Random delay between PDFs in seconds (default: 1,5)")
    parser.add_argument("--output-dir", type=Path, help="PDF root directory (default: project reports/download/)")
    parser.add_argument("--links-only", action="store_true", help="List download URLs without downloading PDFs")
    args = parser.parse_args(argv)
    try:
        result = fetch_report_download_links(args.stock_code, args.timeout)
        if not args.links_only:
            result = download_reports(args.stock_code, result, args.output_dir, args.timeout,
                                      delay_min=args.delay[0], delay_max=args.delay[1])
    except (OSError, ValueError, RuntimeError, URLError) as error:
        parser.exit(1, f"Error: {error}\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
