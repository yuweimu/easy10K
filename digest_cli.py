"""Shared command-line orchestration for financial-statement digesters."""

import argparse
from decimal import Decimal, InvalidOperation
import json
import re
import sys


def nonnegative_decimal(value: str) -> Decimal:
    try:
        number = Decimal(value)
        if not number.is_finite() or number < 0:
            raise ValueError
        return number
    except (InvalidOperation, ValueError):
        raise argparse.ArgumentTypeError("Expected a finite nonnegative number") from None


def pdf_hash_argument(value: str) -> str:
    if not re.fullmatch(r"[0-9a-fA-F]{32}", value.strip()):
        raise argparse.ArgumentTypeError("Expected a 32-character hexadecimal PDF hash")
    return value.strip().lower()


def _print_result(row: dict, result: dict) -> None:
    if result["passed"]:
        print(f"{row['pdf_hash']}: PASS", flush=True)
    else:
        print(json.dumps({key: value for key, value in result.items()
                          if key != "items"}, ensure_ascii=False), flush=True)


def print_equation(pdf_hash: str, item_id: int, item_name: str,
                   amount, expression: str, total, difference, tolerance,
                   passed: bool) -> None:
    if passed:
        return None
    """Print one consistently formatted accounting equation."""
    status = "PASS" if passed else "FAIL"
    color = "32" if passed else "31"
    operator = "≈" if passed else "≠"
    line = (f"{pdf_hash} item-equation ({item_name},{item_id}): "
            f"{amount} {operator} {expression or '0'} = {total}; "
            f"difference={difference}, tolerance={tolerance}: {status}")
    print(f"\033[{color}m{line}\033[0m", flush=True)


def run_digest_cli(statement: str, check_report, load_rows, save_row, argv=None,
                   *, description: str, absolute_tolerance: Decimal,
                   relative_tolerance: Decimal, retry_help: str,
                   allow_dry_run: bool = True, storage_class=None) -> int:
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument("--hash", dest="pdf_hash", type=pdf_hash_argument,
                        help="Process this hash regardless of stage")
    parser.add_argument("--stock_code", help="Process only reports for this stock code")
    parser.add_argument("--retry", action="store_true", help=retry_help)
    parser.add_argument("--ex", action="store_true",
                        help="Digest externally extracted *_ex Markdown through Ex* stages")
    parser.add_argument("--absolute-tolerance", type=nonnegative_decimal,
                        default=absolute_tolerance)
    parser.add_argument("--relative-tolerance", type=nonnegative_decimal,
                        default=relative_tolerance)
    if allow_dry_run:
        parser.add_argument("--dry-run", action="store_true",
                            help="Validate without writing to the database")
    args = parser.parse_args(argv)
    dry_run = getattr(args, "dry_run", False)
    succeeded = failures = 0

    from line_item_chart import LineItemChart
    if storage_class is None:
        from line_item_storage import LineItemStorage as storage_class

    with storage_class() as storage:
        try:
            rows = load_rows(storage, args)
        except Exception as error:
            print(f"Unable to load {statement}: {error}", file=sys.stderr)
            print("Summary: 0 succeeded, 1 failed", flush=True)
            return 1
        if args.pdf_hash and not rows:
            print(f"No report found for PDF hash {args.pdf_hash}", file=sys.stderr)
            print("Summary: 0 succeeded, 1 failed", flush=True)
            return 1

        chart = LineItemChart()
        for row in rows:
            try:
                result = check_report(
                    row, chart,
                    absolute_tolerance=args.absolute_tolerance,
                    relative_tolerance=args.relative_tolerance,
                )
            except Exception as error:
                result = {"pdf_hash": row["pdf_hash"], "statement": statement,
                          "passed": False, "issues": [f"Unable to check report: {error}"],
                          "checks": [], "items": [], "unmapped_rows": []}

            if not dry_run:
                try:
                    save_row(storage, row, result, args)
                except Exception as error:
                    storage.close()
                    result["passed"] = False
                    result["issues"].append(f"Unable to save digestion: {error}")
                    try:
                        save_row(storage, row, result, args, retry_failure=True)
                    except Exception as retry_error:
                        print(f"{row['pdf_hash']}: unable to save failure: {retry_error}",
                              file=sys.stderr)
                        storage.close()

            if result["passed"]:
                succeeded += 1
            else:
                failures += 1
            _print_result(row, result)

    print(f"Summary: {succeeded} succeeded, {failures} failed", flush=True)
    return int(failures > 0)
