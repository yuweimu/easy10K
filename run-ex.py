"""Import externally extracted report files into report_digestion.

The importer reads ``reports/ai-extract/outputs`` files named like::

    600398.2020-12-31.<pdf-md5>.metadata.json
    600398.2020-12-31.<pdf-md5>.balance-sheet.md

Reports already at ``CashFlowOK`` or ``ExCashFlowOK`` are left untouched.
"""

import argparse
from collections import defaultdict
from dataclasses import dataclass
import json
from pathlib import Path
import re
import sys

from line_item_storage import LineItemStorage


PROJECT_DIR = Path(__file__).resolve().parent
DEFAULT_OUTPUTS_DIR = PROJECT_DIR / "reports" / "ai-extract" / "outputs"
DEPRECATED_FILENAME = "deprecated.json"
FILE_PATTERN = re.compile(
    r"^(?P<stock_code>[^.]+)\.(?P<reporting_date>\d{4}-\d{2}-\d{2})\."
    r"(?P<pdf_hash>[0-9a-fA-F]{32})\.(?P<kind>metadata|balance-sheet|"
    r"income-statement|cash-flow-statement)\.(?P<extension>json|md)$"
)
STATEMENT_FIELDS = {
    "balance-sheet": "balance_sheet_ex",
    "income-statement": "income_statement_ex",
    "cash-flow-statement": "cash_flow_statement_ex",
}
COMPLETED_STAGES = {"CashFlowOK", "ExCashFlowOK"}


@dataclass
class ExtractedFiles:
    stock_code: str
    reporting_date: str
    pdf_hash: str
    files: dict[str, Path]


def _collect_files(outputs_dir: Path, stock_code: str | None) -> list[ExtractedFiles]:
    grouped: dict[str, ExtractedFiles] = {}
    for path in sorted(outputs_dir.iterdir()):
        if not path.is_file():
            continue
        match = FILE_PATTERN.fullmatch(path.name)
        if match is None:
            continue
        values = match.groupdict()
        if values["kind"] == "metadata" and values["extension"] != "json":
            continue
        if values["kind"] != "metadata" and values["extension"] != "md":
            continue
        if stock_code and values["stock_code"] != stock_code:
            continue
        pdf_hash = values["pdf_hash"].lower()
        group = grouped.setdefault(
            pdf_hash,
            ExtractedFiles(values["stock_code"], values["reporting_date"], pdf_hash, {}),
        )
        group.files[values["kind"]] = path
    return sorted(grouped.values(), key=lambda group: (group.reporting_date, group.pdf_hash))


def _read_metadata(path: Path | None) -> tuple[str | None, dict]:
    if path is None:
        return None, {}
    text = path.read_text(encoding="utf-8")
    try:
        metadata = json.loads(text)
    except json.JSONDecodeError as error:
        print(f"{path}: invalid metadata JSON: {error}", file=sys.stderr)
        return text, {}
    return text, metadata if isinstance(metadata, dict) else {}


def _report_label(pdf_hash: str, stock_code: str | None, reporting_date: str | None) -> str:
    return f"{pdf_hash} ({stock_code or '?'}, {reporting_date or '?'})"


def _mark_deprecated(storage: LineItemStorage, path: Path, stock_code: str | None) -> int:
    """Mark hashes listed by the extraction scan as Deprecated before import."""
    if not path.is_file():
        return 0
    payload = json.loads(path.read_text(encoding="utf-8"))
    entries = payload.get("files", []) if isinstance(payload, dict) else []
    if not isinstance(entries, list):
        raise ValueError(f"{path}: 'files' must be a list")
    marked = 0
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        if stock_code and str(entry.get("stock_code", "")) != stock_code:
            continue
        pdf_hash = str(entry.get("pdf_md5", "")).strip().lower()
        if not re.fullmatch(r"[0-9a-f]{32}", pdf_hash):
            print(f"{path}: invalid pdf_md5 entry: {entry.get('pdf_md5')!r}", file=sys.stderr)
            continue
        label = _report_label(pdf_hash, str(entry.get("stock_code") or ""),
                              str(entry.get("reporting_date") or ""))
        file_name = str(entry.get("pdf_file_name") or "?")
        stage = storage.get_report_digestion_stage(pdf_hash)
        if stage in COMPLETED_STAGES:
            print(f"{label} [{file_name}]: skipped (already {stage})", flush=True)
            continue
        updated = storage.update_report_digestion_stage(pdf_hash, "Deprecated")
        if updated:
            marked += 1
            print(f"{label} [{file_name}]: marked Deprecated from {path.name}", flush=True)
        else:
            print(f"{label} [{file_name}]: deprecated file is not registered; skipped", flush=True)
    return marked


def _mark_external_extraction_complete(storage: LineItemStorage, pdf_hash: str) -> bool:
    """Set and verify ExExtractOK, tolerating an idempotent second invocation."""
    updated = storage.update_report_digestion_stage(pdf_hash, "ExExtractOK")
    if updated:
        return True
    return storage.get_report_digestion_stage(pdf_hash) == "ExExtractOK"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stock_code", "--stock-code")
    parser.add_argument("--outputs-dir", type=Path, default=DEFAULT_OUTPUTS_DIR)
    args = parser.parse_args(argv)

    if not args.outputs_dir.is_dir():
        print(f"Output directory not found: {args.outputs_dir}", file=sys.stderr)
        return 1

    counts = defaultdict(int)
    with LineItemStorage() as storage:
        if not storage.database_url:
            print("DATABASE_URL is not configured", file=sys.stderr)
            return 1
        deprecated_count = _mark_deprecated(
            storage, args.outputs_dir / DEPRECATED_FILENAME, args.stock_code,
        )
        groups = _collect_files(args.outputs_dir, args.stock_code)
        for group in groups:
            stage = storage.get_report_digestion_stage(group.pdf_hash)
            if stage in COMPLETED_STAGES:
                counts["skipped"] += 1
                print(f"{_report_label(group.pdf_hash, group.stock_code, group.reporting_date)}: "
                      f"skipped (already {stage})", flush=True)
                continue
            metadata_text, metadata = _read_metadata(group.files.get("metadata"))
            metadata_hash = str(metadata.get("pdf_md5", "")).lower()
            label = _report_label(
                group.pdf_hash,
                str(metadata.get("stock_code") or group.stock_code),
                str(metadata.get("reporting_date") or group.reporting_date),
            )
            if metadata_hash and metadata_hash != group.pdf_hash:
                print(f"{label}: metadata hash mismatch", file=sys.stderr)
                counts["skipped"] += 1
                continue

            values = {
                "extraction_report_ex": metadata_text,
                **{
                    field: group.files[kind].read_text(encoding="utf-8")
                    for kind, field in STATEMENT_FIELDS.items()
                    if kind in group.files
                },
            }
            updated = storage.store_extracted_report_extras(
                group.pdf_hash,
                **values,
                stock_code=str(metadata.get("stock_code") or group.stock_code),
                reporting_date=str(metadata.get("reporting_date") or group.reporting_date),
                pdf_creation_date=(metadata.get("pdf_creation_date_raw")
                                   or metadata.get("pdf_creation_date")
                                   or metadata.get("pdf_creation_time")),
            )
            if updated:
                counts["updated"] += 1
                all_extraction_files = set(group.files) == {
                    "metadata", "balance-sheet", "income-statement", "cash-flow-statement",
                }
                if all_extraction_files:
                    _mark_external_extraction_complete(storage, group.pdf_hash)
                    actual_stage = storage.get_report_digestion_stage(group.pdf_hash)
                    print(
                        f"{label}: imported {len(group.files)} extracted file(s); "
                        f"stage={actual_stage or 'unknown'}",
                        flush=True,
                    )
                else:
                    actual_stage = storage.get_report_digestion_stage(group.pdf_hash)
                    print(
                        f"{label}: imported {len(group.files)} extracted file(s); "
                        f"stage={actual_stage or 'unknown'} (incomplete output set)",
                        flush=True,
                    )
            else:
                counts["skipped"] += 1
                print(f"{label}: skipped (unregistered or CashFlowOK/ExCashFlowOK)", flush=True)

    print(f"Marked {deprecated_count} report(s) Deprecated.", flush=True)
    print(f"Found {len(groups)} extracted report group(s).", flush=True)
    print(f"Updated {counts['updated']}; skipped {counts['skipped']}.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
