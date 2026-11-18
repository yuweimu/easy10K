"""Download reports for unmarked stock-list entries, one stock at a time.

Successful lines receive '# DOWNLOADED'; failed lines receive '# ERROR'. Existing
stock directories are reused; completed PDF files are skipped by the downloader.
"""

import argparse
import json
import math
import os
from pathlib import Path
import re
import sys
import tempfile

from pdf_download import DEFAULT_REPORTS_DIR, download_reports, fetch_report_download_links, is_expected_missing_download, validate_delay_range, parse_delay


PROJECT_DIR = Path(__file__).resolve().parent
MARKER = "# DOWNLOADED"


def mark_status(list_path: Path, lines: list[str], index: int, status: str) -> None:
    """Atomically update one line without losing comments, spacing, or newlines."""
    if status not in {"DOWNLOADED", "ERROR"}:
        raise ValueError(f"Invalid stock-list status: {status}")
    original = "".join(lines)
    with list_path.open(encoding="utf-8", newline="") as source:
        if source.read() != original:
            raise RuntimeError(f"{list_path} changed during download; refusing to overwrite it")
    line = lines[index]
    body = line.rstrip("\r\n")
    newline = line[len(body):]
    updated = lines.copy()
    body = re.sub(r"(?:[ \t]+# (?:DOWNLOADED|ERROR))+\s*$", "", body)
    updated[index] = body + " # " + status + newline
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="", dir=list_path.parent,
                                         prefix=f".{list_path.name}.", suffix=".tmp", delete=False) as output:
            temporary = Path(output.name)
            output.write("".join(updated))
            output.flush()
            os.fsync(output.fileno())
        os.chmod(temporary, list_path.stat().st_mode & 0o777)
        os.replace(temporary, list_path)
        lines[:] = updated
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def mark_downloaded(list_path: Path, lines: list[str], index: int) -> None:
    mark_status(list_path, lines, index, "DOWNLOADED")


def run_downloads(stock_list: Path = PROJECT_DIR / "stock_list.txt",
                  reports_dir: Path = DEFAULT_REPORTS_DIR, timeout: float = 30,
                  *, retry_errors: bool = False, delay_min: float = 1, delay_max: float = 5) -> dict[str, int]:
    """Mark completed batches, including expected missing-attachment skips.

    ERROR lines are skipped unless retry_errors is enabled. Existing stock
    directories are reused; only the current attempt determines the new marker.
    """
    stock_list = Path(stock_list).resolve()
    reports_dir = Path(reports_dir).resolve()
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout must be a positive finite number")
    validate_delay_range(delay_min, delay_max)
    with stock_list.open(encoding="utf-8", newline="") as source:
        lines = source.readlines()
    counts = {"downloaded": 0, "skipped": 0, "failed": 0, "invalid": 0}
    completed = set()
    attempted = set()
    failed = set()
    for index in range(len(lines)):
        line = lines[index]
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if re.search(r"\s+# DOWNLOADED\s*$", line):
            counts["skipped"] += 1
            continue
        if not retry_errors and re.search(r"\s+# ERROR\s*$", line):
            counts["skipped"] += 1
            continue
        code = stripped.split()[0]
        if not re.fullmatch(r"[0-9]{6}", code):
            print(f"Line {index + 1}: invalid stock code {code!r}", file=sys.stderr)
            counts["invalid"] += 1
            continue
        if code in completed:
            mark_downloaded(stock_list, lines, index)
            counts["skipped"] += 1
            continue
        if code in failed:
            mark_status(stock_list, lines, index, "ERROR")
            counts["skipped"] += 1
            continue
        if code in attempted:
            print(f"Skipping {code}: already attempted", file=sys.stderr, flush=True)
            counts["skipped"] += 1
            continue
        attempted.add(code)
        print(f"Downloading reports for {code}", file=sys.stderr, flush=True)
        try:
            listings = fetch_report_download_links(code, timeout)
            log_path = reports_dir / code / "downloads.json"
            previous_count = (len(json.loads(log_path.read_text(encoding="utf-8"))["downloads"])
                              if log_path.is_file() else 0)
            result = download_reports(code, listings, reports_dir, timeout,
                                      delay_min=delay_min, delay_max=delay_max)
            entries = result["downloads"][previous_count:]
            if not entries or any(entry.get("status") not in {"DONE", "SKIPPED"}
                                  and not is_expected_missing_download(entry) for entry in entries):
                raise RuntimeError("No reports downloaded, or one or more reports failed")
        except (OSError, ValueError, RuntimeError) as error:
            print(f"Failed {code}: {error}", file=sys.stderr, flush=True)
            mark_status(stock_list, lines, index, "ERROR")
            failed.add(code)
            counts["failed"] += 1
            continue
        mark_downloaded(stock_list, lines, index)
        completed.add(code)
        counts["downloaded"] += 1
    return counts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stock-list", type=Path, default=PROJECT_DIR / "stock_list.txt")
    parser.add_argument("--reports-dir", type=Path, default=DEFAULT_REPORTS_DIR,
                        help="PDF root directory (default: project reports/download/)")
    parser.add_argument("--timeout", type=float, default=30, help="Per-request timeout in seconds")
    parser.add_argument("--retry-errors", action="store_true", help="Retry # ERROR lines")
    parser.add_argument("--delay", type=parse_delay, default=(1, 5), metavar="MIN,MAX",
                        help="Random delay between PDFs in seconds (default: 1,5)")
    args = parser.parse_args(argv)
    try:
        counts = run_downloads(args.stock_list, args.reports_dir, args.timeout, retry_errors=args.retry_errors,
                               delay_min=args.delay[0], delay_max=args.delay[1])
    except (OSError, ValueError, RuntimeError) as error:
        parser.exit(1, f"Error: {error}\n")
    print("; ".join(f"{key}: {value}" for key, value in counts.items()))
    return int(bool(counts["failed"] or counts["invalid"]))


if __name__ == "__main__":
    raise SystemExit(main())
