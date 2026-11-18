"""Validate and digest the first amount column of AuditPass balance sheets.

Run: python digest_line_items.py
Amounts are checked in statement units, then converted to yuan for storage.
The default tolerance is the greater of 0.01 statement units and 0.1 percent.
"""

import argparse
from datetime import date
from decimal import Decimal, InvalidOperation
import json
import re
import sys

from line_item_chart import LineItemChart
from line_item_storage import LineItemStorage


REQUIRED_IDS = (11, 
                1101, 110101, 110138, 110150, 
                1102, 110231, 110242, 110266,
                21, 2101, 2102, 
                31, 3110, 311001, 311004, 311007, 311008)
UNITS = {"元": 1, "千元": 1000, "万元": 10000, "百万元": 1000000, "亿元": 100000000}
NUMBER = re.compile(r"[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?")


def clean_cell(text: str) -> str:
    text = re.sub(r"<br\s*/?>", "", text, flags=re.I)
    return re.sub(r"\s+", "", text).replace("*", "").replace("，", ",").replace("−", "-")


def parse_amount(text: str) -> Decimal | None:
    value = clean_cell(text)
    if value in ("", "-", "--", "—", "–"):
        return None
    if value.startswith(("(", "（")) and value.endswith((")", "）")):
        value = "-" + value[1:-1]
    if not NUMBER.fullmatch(value):
        raise ValueError(f"Invalid amount: {text!r}")
    return Decimal(value.replace(",", ""))


def check_report(row: dict, chart: LineItemChart, *, absolute_tolerance: Decimal = Decimal("0.001"),
                 relative_tolerance: Decimal = Decimal("0.0001")) -> dict:
    """Return JSON-serializable parsing details, checks, and proposed line items."""
    result = {"pdf_hash": row["pdf_hash"], "passed": False, "issues": [], "checks": [],
              "items": [], "unmapped_rows": [],
              "tolerance": {"absolute_statement_units": str(absolute_tolerance),
                            "relative": str(relative_tolerance)}}
    issues, checks = result["issues"], result["checks"]
    markdown = row.get("balance_sheet") or ""
    if not markdown.strip():
        issues.append("Missing balance_sheet Markdown")
    for field in ("stock_code", "reporting_date", "pdf_creation_date"):
        if not row.get(field):
            issues.append(f"Missing {field}")
    if row.get("reporting_date"):
        try:
            date.fromisoformat(str(row["reporting_date"]))
        except ValueError:
            issues.append("Invalid reporting_date")
    compact = clean_cell(markdown)
    units = set(re.findall(r"单位[：:（(]?(?:均为|为)?(?:人民币)?(百万元|亿元|万元|千元|元)", compact))
    multiplier = UNITS[next(iter(units))] if len(units) == 1 else None
    result["amount_unit"] = next(iter(units)) if len(units) == 1 else None
    if multiplier is None:
        issues.append("Missing or conflicting currency unit")
    amount_column = 1
    parsed = {}
    for line_number, line in enumerate(markdown.splitlines(), 1):
        if not line.strip().startswith("|"):
            continue
        cells = [cell.strip() for cell in re.split(r"(?<!\\)\|", line.strip().strip("|"))]
        if len(cells) < 2:
            continue
        label = clean_cell(cells[0]).replace("(", "（").replace(")", "）")
        if label in ("项目", "科目"):
            amount_column = 2 if re.search(r"附注|注释|行次|^注$", clean_cell(cells[1])) else 1
            continue
        if not label or re.fullmatch(r"[:\-]+", label):
            continue
        label = re.sub(r"^(?:[一二三四五六七八九十\d]+[、.．]|[（(][一二三四五六七八九十\d]+[）)])", "", label)
        label = label.rstrip("：:")
        item = chart.get(label)
        if item is None or item.item_id == 99000001:
            continue_if_total = re.search(r"负债[和及].*权益.*总计", label)
            if item is None and not continue_if_total:
                result["unmapped_rows"].append({"line": line_number, "label": label})
            continue
        if len(cells) <= amount_column:
            issues.append(f"Line {line_number}: missing first amount column for {label}")
            continue
        try:
            amount = parse_amount(cells[amount_column])
        except ValueError as error:
            issues.append(f"Line {line_number}: {label}: {error}")
            continue
        previous = parsed.get(item.item_id)
        if previous is not None and previous[1] is not None:
            if amount is not None and previous[1] != amount:
                issues.append(f"Conflicting amounts for item {item.item_id}: {previous[1]} and {amount}")
            continue
        parsed[item.item_id] = (item, amount)
    failed_required_ids = []
    for item_id in REQUIRED_IDS:
        amount = parsed[item_id][1] if item_id in parsed else None
        if amount is None or amount == 0:
            failed_required_ids.append(item_id)
    checks.append({"check": "required_nonzero", "checked": len(REQUIRED_IDS),
                   "passed": not failed_required_ids, "failed_item_ids": failed_required_ids})
    if failed_required_ids:
        issues.append("Required items missing, empty, or zero: " + ", ".join(map(str, failed_required_ids)))
    amounts = {item_id: amount for item_id, (_, amount) in parsed.items() if amount is not None}
    for parent_id, parent_amount in amounts.items():
        prefix = str(parent_id)
        if len(prefix) not in (2, 4):
            continue
        children = [item_id for item_id in amounts
                    if str(item_id).startswith(prefix) and len(str(item_id)) == len(prefix) + 2]
        if not children:
            continue
        child_amounts = [-abs(amounts[item_id]) if item_id == 311003 else amounts[item_id]
                         for item_id in children]
        total = sum(child_amounts, Decimal(0))
        difference = abs(parent_amount - total)
        tolerance = max(absolute_tolerance, relative_tolerance * max(abs(parent_amount), abs(total)))
        passed = difference <= tolerance
        expression = " + ".join(
            (f"({amount})" if amount < 0 else str(amount)) + f"({parsed[item_id][0].name}, {item_id})"
            for item_id, amount in zip(children, child_amounts))
        print(f"{row['pdf_hash']} item {parent_id} ({parsed[parent_id][0].name}): "
              f"{parent_amount} {'≈' if passed else '≠'} "
              f"{expression} = {total}; difference={difference}, tolerance={tolerance}: "
              f"{'PASS' if passed else 'FAIL'}", flush=True)
        checks.append({"check": "parent_sum", "item_id": parent_id, "children": sorted(children),
                       "amount": str(parent_amount), "children_sum": str(total),
                       "difference": str(difference), "tolerance": str(tolerance), "passed": passed})
        if not passed:
            issues.append(f"Item {parent_id}: {parent_amount} differs from immediate children sum {total}")
    for item_id, (item, amount) in parsed.items():
        if amount is not None:
            result["items"].append({"item_id": item_id, "english_name": item.english_name,
                                    "statement_amount": str(amount),
                                    "amount": str(amount * multiplier) if multiplier is not None else None})
    result["passed"] = not issues
    return result


def nonnegative_decimal(value: str) -> Decimal:
    try:
        number = Decimal(value)
        if not number.is_finite() or number < 0:
            raise ValueError
        return number
    except (InvalidOperation, ValueError):
        raise argparse.ArgumentTypeError("Expected a finite nonnegative number") from None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--absolute-tolerance", type=nonnegative_decimal, default=Decimal("0.01"))
    parser.add_argument("--relative-tolerance", type=nonnegative_decimal, default=Decimal("0.001"))
    args = parser.parse_args(argv)
    chart = LineItemChart()
    failed = False
    with LineItemStorage() as storage:
        try:
            rows = storage.get_audit_pass_reports()
        except Exception as error:
            print(f"Unable to load AuditPass reports: {error}", file=sys.stderr)
            return 1
        for row in rows:
            try:
                result = check_report(row, chart, absolute_tolerance=args.absolute_tolerance,
                                      relative_tolerance=args.relative_tolerance)
            except Exception as error:
                result = {"pdf_hash": row["pdf_hash"], "passed": False, "checks": [], "items": [],
                          "issues": [f"Unable to check report: {error}"]}
            try:
                print("skip writing db")
                # storage.store_line_item_digestion(row, result)
            except Exception as error:
                storage.close()
                result["passed"] = False
                result["issues"].append(f"Unable to save digestion: {error}")
                try:
                    storage.store_line_item_digestion(row, result)
                except Exception as save_error:
                    print(f"{row['pdf_hash']}: unable to save failure: {save_error}", file=sys.stderr)
                    storage.close()

            failed |= not result["passed"]
            print(json.dumps({key: value for key, value in result.items() if key != "items"},
                             ensure_ascii=False), flush=True)

            print(f"quit loop now. PASSED={not failed}")
            return int(failed) # temp
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
