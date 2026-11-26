"""Resume extraction and digestion for every registered report of one stock.

Usage:
    python run.py 300750

Each report is advanced from its current digestion stage. Deprecated reports are
ignored. Reports ending at ExtractErr or AuditErr are copied to the AI review
directory after processing.
"""

import argparse
from collections import Counter
from pathlib import Path
import shutil
import sys

import audit_extraction
import digest_bs
import digest_cf
import digest_is
from extract import _extract_one_report
from line_item_storage import LineItemStorage


PROJECT_DIR = Path(__file__).resolve().parent
DEFAULT_AI_EXTRACT_DIR = PROJECT_DIR / "reports" / "ai-extract"
TERMINAL_STAGES = {"CashFlowOK", "ExCashFlowOK", "FileErr", "NoStatements"}
REPORT_STAGES = (
    "FileOK", "FileErr", "ExtractOK", "ExtractErr", "AuditOK", "AuditErr",
    "Deprecated", "NoStatements", "BalanceOK", "BalanceErr", "IncomeOK",
    "IncomeErr", "CashFlowOK", "CashFlowErr", "ExExtractOK", "ExExtractErr",
    "ExAuditOK", "ExAuditErr", "ExBalanceOK", "ExBalanceErr", "ExIncomeOK",
    "ExIncomeErr", "ExCashFlowOK", "ExCashFlowErr",
)


def _refresh_report(storage: LineItemStorage, stock_code: str, pdf_hash: str) -> dict:
    report = next(
        (row for row in storage.get_report_pipeline_rows(stock_code)
         if row["pdf_hash"] == pdf_hash),
        None,
    )
    if report is None:
        raise ValueError(f"Report {pdf_hash} disappeared from report_digestion")
    return report


def _run_digest(module, pdf_hash: str) -> int:
    """Run one statement digester against a single registered report."""
    return module.main(["--hash", pdf_hash])


def _release_storage_connection(storage: LineItemStorage) -> None:
    """Release the runner's read transaction before opening a worker connection.

    The digesters run in-process and create their own ``LineItemStorage``.
    PostgreSQL keeps the runner's SELECT transaction open until it is
    committed or closed; the digester's idempotent schema setup contains DDL
    and otherwise waits indefinitely for that transaction's table lock.
    ``LineItemStorage`` reconnects lazily when the runner refreshes the row.
    """
    storage.close()


def _process_report(storage: LineItemStorage, report: dict, stock_code: str) -> None:
    """Advance one report, stopping after a stage remains in an error state."""
    attempted_stages: set[str] = set()
    while True:
        stage = report["digestion_stage"]
        pdf_hash = report["pdf_hash"]
        path = report.get("local_path")
        if stage in TERMINAL_STAGES:
            print(f"{pdf_hash}: {stage}")
            return
        if stage == "Deprecated":
            return
        if stage in attempted_stages:
            print(f"{pdf_hash}: stopped at {stage}", file=sys.stderr)
            return
        attempted_stages.add(stage)

        if stage in {"FileOK", "ExtractErr"}:
            print(f"{pdf_hash}: extracting {path}", flush=True)
            try:
                _extract_one_report(storage, report, None, PROJECT_DIR)
            except Exception as error:
                print(f"{pdf_hash}: extraction failed: {error}", file=sys.stderr)
                storage.close()
                try:
                    storage.update_report_digestion_stage(pdf_hash, "ExtractErr")
                except Exception as stage_error:
                    print(f"{pdf_hash}: unable to save ExtractErr: {stage_error}", file=sys.stderr)
                return
            report = _refresh_report(storage, stock_code, pdf_hash)
            continue

        if stage in {"ExtractOK", "AuditErr"}:
            print(f"{pdf_hash}: auditing extraction", flush=True)
            _release_storage_connection(storage)
            audit_extraction.audit_extracted_reports(
                PROJECT_DIR / "extracted" / "pdf",
                pdf_hash=pdf_hash,
                stock_code=stock_code,
                retry=stage == "AuditErr",
            )
            report = _refresh_report(storage, stock_code, pdf_hash)
            continue

        if stage in {"AuditOK", "ManualPass", "BalanceErr"}:
            print(f"{pdf_hash}: digesting balance sheet", flush=True)
            _release_storage_connection(storage)
            return_code = _run_digest(digest_bs, pdf_hash)
            report = _refresh_report(storage, stock_code, pdf_hash)
            if return_code:
                return
            continue

        if stage in {"BalanceOK", "IncomeErr"}:
            print(f"{pdf_hash}: digesting income statement", flush=True)
            _release_storage_connection(storage)
            return_code = _run_digest(digest_is, pdf_hash)
            report = _refresh_report(storage, stock_code, pdf_hash)
            if return_code:
                return
            continue

        if stage in {"IncomeOK", "CashFlowErr"}:
            print(f"{pdf_hash}: digesting cash-flow statement", flush=True)
            _release_storage_connection(storage)
            return_code = _run_digest(digest_cf, pdf_hash)
            report = _refresh_report(storage, stock_code, pdf_hash)
            if return_code:
                return
            continue

        print(f"{pdf_hash}: unsupported digestion stage {stage}", file=sys.stderr)
        return


def _copy_error_reports(rows: list[dict], destination: Path) -> int:
    destination.mkdir(parents=True, exist_ok=True)
    copied = 0
    for row in rows:
        if row["digestion_stage"] not in {"ExtractErr", "AuditErr"}:
            continue
        local_path = row.get("local_path")
        if not local_path:
            print(f"{row['pdf_hash']}: error stage has no local path", file=sys.stderr)
            continue
        source = Path(local_path)
        if not source.is_absolute():
            source = PROJECT_DIR / source
        source = source.resolve()
        if not source.is_file():
            print(f"{row['pdf_hash']}: PDF not found at {source}", file=sys.stderr)
            continue
        target = destination / source.name
        if target.exists():
            target = destination / f"{row['pdf_hash']}_{source.name}"
        shutil.copy2(source, target)
        copied += 1
        print(f"Copied {row['digestion_stage']}: {source} -> {target}", flush=True)
    return copied


def _print_stage_summary(stock_code: str, rows: list[dict]) -> None:
    """Print the final non-deprecated file count and every digestion-stage count."""
    counts = Counter(row.get("digestion_stage") for row in rows)
    reset = "\033[0m"
    bold_purple = "\033[1;35m"
    red = "\033[31m"
    green = "\033[32m"
    print(f"Summary for {stock_code}:", flush=True)
    print(f"{bold_purple}  Total non-deprecated files: {len(rows)}{reset}", flush=True)
    for stage in REPORT_STAGES:
        count = counts.get(stage, 0)
        color = red if stage.endswith("Err") and count else green if stage in {"CashFlowOK", "ExCashFlowOK"} else ""
        suffix = reset if color else ""
        print(f"{color}  {stage}: {count}{suffix}", flush=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stock_code",
                        help="Process all registered reports for this stock code")
    parser.add_argument("--ai-extract-dir", type=Path, default=DEFAULT_AI_EXTRACT_DIR,
                        help=f"Directory for ExtractErr/AuditErr PDFs (default: {DEFAULT_AI_EXTRACT_DIR})")
    args = parser.parse_args(argv)

    with LineItemStorage() as storage:
        if not storage.database_url:
            print("DATABASE_URL is not configured", file=sys.stderr)
            return 1
        try:
            reports = storage.get_report_pipeline_rows(args.stock_code)
        except Exception as error:
            print(f"Unable to load reports for {args.stock_code}: {error}", file=sys.stderr)
            return 1
        print(f"Found {len(reports)} non-deprecated reports for {args.stock_code}.", flush=True)
        for report in reports:
            _process_report(storage, report, args.stock_code)
        final_reports = storage.get_report_pipeline_rows(args.stock_code)

    copied = _copy_error_reports(final_reports, args.ai_extract_dir.resolve())
    error_count = sum(row["digestion_stage"] in {"ExtractErr", "AuditErr"} for row in final_reports)
    print(f"Finished {args.stock_code}: {error_count} error-stage reports; copied {copied} PDF(s).", flush=True)
    _print_stage_summary(args.stock_code, final_reports)
    return int(error_count > 0)


if __name__ == "__main__":
    raise SystemExit(main())
