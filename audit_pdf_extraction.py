"""Audit completed database extractions or a PDF corpus, saving audit.json.

This is a screening tool, not a cell-by-cell certification of financial data.
"""
import argparse
from collections import Counter
from dataclasses import asdict
import json
from pathlib import Path
import re

from pdf_extract import STATEMENTS, ReportExtraction, Statement, extract_report
from line_item_storage import LineItemStorage

PROJECT_DIR = Path(__file__).resolve().parent
NUMBER = re.compile(r"(?<![\d.])[-−]?\d[\d,]*\.\d{2,}(?!\d)")


def audit_report(report: ReportExtraction, pdf_path: Path) -> dict:
    """Check metadata, statement structure, boundaries, and decimal coverage."""
    entry = {"file": str(pdf_path), "issues": [], "statements": {}}
    for key in ("stock_code", "reporting_date", "pdf_creation_time"):
        entry[key] = getattr(report, key)
        if not entry[key]:
            entry["issues"].append("missing " + key)
    year = re.search(r"(?<!\d)(20\d{2})(?!\d)", pdf_path.stem)
    if year:
        quarter = re.search(r"q([1234])", pdf_path.stem, re.I)
        if quarter:
            suffix = {"1": "03-31", "2": "06-30", "3": "09-30", "4": "12-31"}[quarter.group(1)]
        else:
            suffix = "06-30" if "半年度" in pdf_path.stem else "12-31"
        expected = year.group(1) + "-" + suffix
        if report.reporting_date and report.reporting_date != expected:
            entry["issues"].append(f"date differs from filename: {expected}")
    for key in dict.fromkeys(STATEMENTS.values()):
        statement = getattr(report, key)
        if statement is None:
            entry["issues"].append("missing " + key)
            continue
        text_numbers = Counter(NUMBER.findall(statement.text))
        row_numbers = Counter(NUMBER.findall(" ".join(" ".join(row) for row in statement.rows)))
        missing = text_numbers - row_numbers
        count = sum(text_numbers.values())
        missing_count = sum(missing.values())
        entry["statements"][key] = {
            "pages": statement.pages, "rows": len(statement.rows),
            "column_counts": sorted(set(map(len, statement.rows))),
            "source_decimal_count": count, "missing_decimal_count": missing_count,
            "missing_decimal_examples": list(missing)[:8],
        }
        if len(statement.rows) < 10:
            entry["issues"].append(f"{key}: too few rows ({len(statement.rows)})")
        if len(statement.pages) > 7:
            entry["issues"].append(f"{key}: unusually long ({len(statement.pages)} pages)")
        if missing_count >= max(3, count * .05):
            entry["issues"].append(f"{key}: decimals missing from rows ({missing_count}/{count})")
        if any(len(row) < 3 for row in statement.rows):
            entry["issues"].append(f"{key}: fewer than 3 columns")
    return entry


def load_cached_report(path: Path) -> ReportExtraction:
    """Load saved extraction data, including older results without pdf_hash."""
    return load_report_json(path.read_text(encoding="utf-8"))


def load_report_json(payload: str) -> ReportExtraction:
    """Reconstruct statement objects from a saved extraction JSON payload."""
    data = json.loads(payload)
    for key in dict.fromkeys(STATEMENTS.values()):
        if data[key] is not None:
            data[key] = Statement(**data[key])
    return ReportExtraction(**data)


def write_json(path: Path, data: object) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def audit_extracted_reports(output_dir: Path) -> list[dict]:
    """Audit ExtractionSuccess rows using stored JSON; source PDFs are optional."""
    with LineItemStorage() as storage:
        rows = storage.get_extracted_reports()
    output_dir = Path(output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    results = []
    write_json(output_dir / "audit.json", results)
    for index, row in enumerate(rows, 1):
        entry = {"pdf_hash": row["pdf_hash"], "file": row["local_path"],
                 "issues": [], "statements": {}}
        
        try:
            report = load_report_json(row["extraction_report"])
            path = Path(row["local_path"] or report.pdf_file)
            entry.update(audit_report(report, path))
        except Exception as error:
            entry["error"] = repr(error)
            entry["issues"].append("extraction exception")
        results.append(entry)
        print(f"{index}/{len(rows)} {row['pdf_hash']}: "
              + ("; ".join(entry["issues"]) or "PASS"), flush=True)
        write_json(output_dir / "audit.json", results)
    print(f"TOTAL {len(results)}; flagged {sum(bool(r['issues']) for r in results)}", flush=True)
    return results


def audit_reports(input_dir: Path, output_dir: Path, *, cached: bool = False) -> list[dict]:
    """Recursively extract PDFs, continuing after errors and saving progress.

    Cached mode trusts existing results without checking source freshness. Omit
    it after changing PDFs or extraction logic. Output subdirectories mirror the
    input tree; existing generated report files are replaced.
    """
    input_dir = Path(input_dir).resolve()
    output_dir = Path(output_dir).resolve()
    if not input_dir.is_dir():
        raise NotADirectoryError(input_dir)
    files = sorted(path for path in input_dir.rglob("*")
                   if path.is_file() and path.suffix.lower() == ".pdf")
    output_dir.mkdir(parents=True, exist_ok=True)
    results = []
    write_json(output_dir / "audit.json", results)
    for index, path in enumerate(files, 1):
        try:
            relative_path = path.relative_to(PROJECT_DIR)
        except ValueError:
            relative_path = path
        entry = {"file": str(relative_path), "issues": [], "statements": {}}
        try:
            folder = output_dir / path.relative_to(input_dir).with_suffix("")
            cache = folder / "report.json"
            report = load_cached_report(cache) if cached and cache.exists() else extract_report(path)
            entry = audit_report(report, relative_path)
            folder.mkdir(parents=True, exist_ok=True)
            write_json(cache, asdict(report))
            for key in STATEMENTS.values():
                statement = getattr(report, key)
                if statement is not None:
                    (folder / f"{key}.md").write_text(statement.markdown(), encoding="utf-8")
        except Exception as error:
            entry["error"] = repr(error)
            entry["issues"].append("extraction exception")
        results.append(entry)
        print(f"{index}/{len(files)} {path.name}: " + ("; ".join(entry["issues"]) or "PASS"), flush=True)
        write_json(output_dir / "audit.json", results)
    print(f"TOTAL {len(results)}; flagged {sum(bool(r['issues']) for r in results)}", flush=True)
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-dir", type=Path,
                        help="Extract PDFs from this directory instead of auditing database rows")
    parser.add_argument("--output-dir", type=Path, default=PROJECT_DIR / "extracted/pdf")
    parser.add_argument("--cached", action="store_true",
                        help="Trust saved report.json results; extract only uncached PDFs")
    parser.add_argument("--fail-on-issues", action="store_true",
                        help="Return exit code 1 if any report has audit issues")
    args = parser.parse_args(argv)
    if args.input_dir is not None:
        if not args.input_dir.is_dir():
            parser.error(f"Input directory does not exist: {args.input_dir}")
        results = audit_reports(args.input_dir, args.output_dir, cached=args.cached)
    else:
        if args.cached:
            parser.error("--cached requires --input-dir; database mode always uses stored extractions")
        results = audit_extracted_reports(args.output_dir)
    return int(args.fail_on_issues and any(entry["issues"] for entry in results))


if __name__ == "__main__":
    raise SystemExit(main())
