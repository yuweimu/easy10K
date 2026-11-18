"""Validate and digest the first amount column of AuditPass balance sheets.

Run: python digest_line_items.py [--hash PDF_HASH]
Amounts are checked in statement units, then converted to yuan for storage.
The default tolerance is the greater of 0.01 statement units and 0.1 percent.
"""

import argparse
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
import json
import re
import sys

from line_item_chart import LineItem, LineItemChart
from line_item_storage import LineItemStorage


REQUIRED_IDS = (11, 
                1101, 110101, 110128, 
                1102, 110231, 110242,
                21, 2101, 2102, 
                31, 3110, 311001, 311004, 311008)
UNITS = {"元": 1, "千元": 1000, "万元": 10000, "百万元": 1000000, "亿元": 100000000}
NUMBER = re.compile(r"[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?")


def clean_cell(text: str) -> str:
    text = re.sub(r"<br\s*/?>", "", text, flags=re.I)
    return re.sub(r"\s+", "", text).replace("*", "").replace("，", ",").replace("−", "-")


def parse_amount(text: str) -> Decimal | None:
    value = clean_cell(text)
    if value in ("/", "-"):
        return Decimal(0)
    if value in ("", "--", "—", "–"):
        return None
    if value.startswith(("(", "（")) and value.endswith((")", "）")):
        value = "-" + value[1:-1]
    if not NUMBER.fullmatch(value):
        raise ValueError(f"Invalid amount: {text!r}")
    return Decimal(value.replace(",", ""))


@dataclass
class ParsedBalanceSheet:
    """Parsed amounts and the evidence needed to resolve shifted columns."""

    items: dict[int, tuple[LineItem, Decimal | None]] = field(default_factory=dict)
    ambiguous_rows: dict[int, int] = field(default_factory=dict)
    shifted_items: set[int] = field(default_factory=set)
    issues: list[str] = field(default_factory=list)
    unmapped_rows: list[dict] = field(default_factory=list)


def check_report_metadata(row: dict, result: dict) -> tuple[str, int | None]:
    """Validate report metadata and determine the displayed currency unit."""
    issues = result["issues"]
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
    return markdown, multiplier


def select_amount_column(cells: list[str], amount_column: int) -> int:
    """Recognize monetary values shifted into the nominal notes column."""
    row_amount_column = amount_column
    if amount_column == 2:
        # Continued PDF tables can lose the notes column while retaining
        # its header and padding at the end of each extracted row. A
        # monetary value here is the current amount, not a note reference.
        candidate = clean_cell(cells[1]).strip("()（）")
        if (NUMBER.fullmatch(candidate) and
                ("," in candidate or re.search(r"\.\d{2,}$", candidate))):
            row_amount_column = 1
    return row_amount_column


def parse_balance_sheet(markdown: str, chart: LineItemChart) -> ParsedBalanceSheet:
    """Map Markdown rows to items, retaining column-shift evidence and errors."""
    amount_column = 1
    state = ParsedBalanceSheet()
    parsed, ambiguous_rows, shifted_items = state.items, state.ambiguous_rows, state.shifted_items
    issues = state.issues
    for line_number, line in enumerate(markdown.splitlines(), 1):
        if not line.strip().startswith("|"):
            continue
        cells = [cell.strip() for cell in re.split(r"(?<!\\)\|", line.strip().strip("|"))]
        if len(cells) < 2:
            continue
        label = clean_cell(re.sub(r"【[^】]*(?:】|$)", "", cells[0])).replace("(", "（").replace(")", "）")
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
                state.unmapped_rows.append({"line": line_number, "label": label})
            continue
        row_amount_column = select_amount_column(cells, amount_column)
        if len(cells) <= row_amount_column:
            issues.append(f"Line {line_number}: missing first amount column for {label}")
            continue
        try:
            amount = parse_amount(cells[row_amount_column])
        except ValueError as error:
            issues.append(f"Line {line_number}: {label}: {error}")
            continue
        previous = parsed.get(item.item_id)
        if previous is not None and previous[1] is not None:
            if amount is not None and previous[1] != amount:
                issues.append(f"Conflicting amounts for item {item.item_id}: {previous[1]} and {amount}")
            continue
        parsed[item.item_id] = (item, amount)
        if amount is not None and row_amount_column == 1 and amount_column == 2:
            shifted_items.add(item.item_id)
        if (amount is not None and amount_column == row_amount_column == 2
                and not clean_cell(cells[1]) and len(cells) >= 4 and not clean_cell(cells[-1])):
            ambiguous_rows[item.item_id] = line_number
    return state


def subtotal_children(parent_id: int, amounts: dict[int, Decimal],
                      reporting_date: str | date | None) -> list[int]:
    """Select subtotal children using the hierarchy for the report's period."""
    if parent_id == 110110:
        return [110111, 110113] if 110111 in amounts and 110113 in amounts else []
    prefix = str(parent_id)
    if len(prefix) not in (2, 4):
        return []
    children = [item_id for item_id in amounts
                if str(item_id).startswith(prefix) and len(str(item_id)) == len(prefix) + 2]
    if parent_id == 1101 and 110111 in amounts and 110113 in amounts:
        children = [item_id for item_id in children if item_id != 110110]
    legacy_children = {1101: (11012801, 11012802), 2101: (21013501, 21013502)}
    if parent_id in legacy_children:
        try:
            legacy_hierarchy = date.fromisoformat(str(reporting_date)) < date(2017, 12, 31)
        except ValueError:
            # Missing or invalid dates are reported by metadata validation.
            legacy_hierarchy = False
        if legacy_hierarchy:
            children.extend(item_id for item_id in legacy_children[parent_id] if item_id in amounts)
    return children


def resolve_shifted_amounts(state: ParsedBalanceSheet, absolute_tolerance: Decimal,
                            relative_tolerance: Decimal, *,
                            reporting_date: str | date | None = None) -> list[dict]:
    """Omit a possible prior-period amount only when one omission reconciles."""
    parsed, ambiguous_rows, shifted_items = state.items, state.ambiguous_rows, state.shifted_items
    # A blank notes/current cell followed by one number can also be a shifted
    # row with only a prior-period amount. Resolve it against its subtotal only
    # when the surrounding group demonstrably contains shifted rows.
    column_adjustments = []
    for parent_id, (_, parent_amount) in list(parsed.items()):
        if parent_amount is None or len(str(parent_id)) not in (2, 4):
            continue
        amounts = {item_id: amount for item_id, (_, amount) in parsed.items() if amount is not None}
        children = {item_id: amounts[item_id]
                    for item_id in subtotal_children(parent_id, amounts, reporting_date)}
        if not (shifted_items & (set(children) | {parent_id})):
            continue
        contributions = {item_id: -abs(amount) if item_id == 311003 else amount
                         for item_id, amount in children.items()}
        total = sum(contributions.values(), Decimal(0))
        tolerance = max(absolute_tolerance, relative_tolerance * max(abs(parent_amount), abs(total)))
        if abs(parent_amount - total) <= tolerance:
            continue
        alternatives = []
        for item_id, contribution in contributions.items():
            if item_id not in ambiguous_rows or contribution == 0:
                continue
            alternative = total - contribution
            tolerance = max(absolute_tolerance, relative_tolerance * max(abs(parent_amount), abs(alternative)))
            if abs(parent_amount - alternative) <= tolerance:
                alternatives.append((item_id, alternative))
        if len(alternatives) == 1:
            item_id, alternative = alternatives[0]
            item, amount = parsed[item_id]
            parsed[item_id] = (item, None)
            column_adjustments.append({"item_id": item_id, "line": ambiguous_rows[item_id],
                                       "parent_id": parent_id, "excluded_amount": str(amount),
                                       "sum_with": str(total), "sum_without": str(alternative),
                                       "reason": "Possible prior-period value in a shifted row; only omission reconciles subtotal"})
    return column_adjustments


def check_required_items(parsed: dict[int, tuple[LineItem, Decimal | None]], result: dict) -> None:
    """Summarize the required nonzero checks and record any failures."""
    issues, checks = result["issues"], result["checks"]
    failed_required_ids = []
    for item_id in REQUIRED_IDS:
        amount = parsed[item_id][1] if item_id in parsed else None
        if amount is None or amount == 0:
            failed_required_ids.append(item_id)
    checks.append({"check": "required_nonzero", "checked": len(REQUIRED_IDS),
                   "passed": not failed_required_ids, "failed_item_ids": failed_required_ids})
    if failed_required_ids:
        issues.append("Required items missing, empty, or zero: " + ", ".join(map(str, failed_required_ids)))


def check_parent_totals(row: dict, parsed: dict[int, tuple[LineItem, Decimal | None]],
                        result: dict, absolute_tolerance: Decimal, relative_tolerance: Decimal) -> None:
    """Validate subtotals and print their arithmetic equations."""
    issues, checks = result["issues"], result["checks"]
    amounts = {item_id: amount for item_id, (_, amount) in parsed.items() if amount is not None}
    for parent_id, parent_amount in amounts.items():
        children = subtotal_children(parent_id, amounts, row.get("reporting_date"))
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


def build_line_items(parsed: dict[int, tuple[LineItem, Decimal | None]], multiplier: int | None) -> list[dict]:
    """Convert validated statement amounts to proposed database item values."""
    items = []
    for item_id, (item, amount) in parsed.items():
        if amount is not None:
            items.append({"item_id": item_id, "english_name": item.english_name,
                          "statement_amount": str(amount),
                          "amount": str(amount * multiplier) if multiplier is not None else None})
    return items


def check_report(row: dict, chart: LineItemChart, *, absolute_tolerance: Decimal = Decimal("0.001"),
                 relative_tolerance: Decimal = Decimal("0.0001")) -> dict:
    """Return JSON-serializable parsing details, checks, and proposed line items."""
    result = {"pdf_hash": row["pdf_hash"], "passed": False, "issues": [], "checks": [],
              "items": [], "unmapped_rows": [],
              "tolerance": {"absolute_statement_units": str(absolute_tolerance),
                            "relative": str(relative_tolerance)}}
    markdown, multiplier = check_report_metadata(row, result)
    state = parse_balance_sheet(markdown, chart)
    result["issues"].extend(state.issues)
    result["unmapped_rows"] = state.unmapped_rows
    adjustments = resolve_shifted_amounts(state, absolute_tolerance, relative_tolerance,
                                          reporting_date=row.get("reporting_date"))
    if adjustments:
        result["column_adjustments"] = adjustments
    if state.unmapped_rows:
        result["issues"].append(f"Unmapped balance-sheet rows: {len(state.unmapped_rows)}")
    check_required_items(state.items, result)
    check_parent_totals(row, state.items, result, absolute_tolerance, relative_tolerance)
    result["items"] = build_line_items(state.items, multiplier)
    result["passed"] = not result["issues"]
    return result


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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hash", dest="pdf_hash", type=pdf_hash_argument,
                        help="Digest only this PDF hash, regardless of its current stage")
    parser.add_argument("--absolute-tolerance", type=nonnegative_decimal, default=Decimal("0.01"))
    parser.add_argument("--relative-tolerance", type=nonnegative_decimal, default=Decimal("0.001"))
    args = parser.parse_args(argv)
    save_options = {"allow_any_stage": True} if args.pdf_hash else {}
    chart = LineItemChart()
    failed = False
    with LineItemStorage() as storage:
        try:
            rows = (storage.get_audit_pass_reports(pdf_hash=args.pdf_hash) if args.pdf_hash
                    else storage.get_audit_pass_reports())
        except Exception as error:
            print(f"Unable to load reports for digestion: {error}", file=sys.stderr)
            return 1
        if args.pdf_hash and not rows:
            print(f"No report found for PDF hash {args.pdf_hash}", file=sys.stderr)
            return 1
        for row in rows:
            try:
                result = check_report(row, chart, absolute_tolerance=args.absolute_tolerance,
                                      relative_tolerance=args.relative_tolerance)
            except Exception as error:
                result = {"pdf_hash": row["pdf_hash"], "passed": False, "checks": [], "items": [],
                          "issues": [f"Unable to check report: {error}"]}
            try:
                # print("skip writing db")
                storage.store_line_item_digestion(row, result, **save_options)
            except Exception as error:
                storage.close()
                result["passed"] = False
                result["issues"].append(f"Unable to save digestion: {error}")
                try:
                    storage.store_line_item_digestion(row, result, **save_options)
                except Exception as save_error:
                    print(f"{row['pdf_hash']}: unable to save failure: {save_error}", file=sys.stderr)
                    storage.close()

            failed |= not result["passed"]
            print(json.dumps({key: value for key, value in result.items() if key != "items"},
                             ensure_ascii=False), flush=True)

            print(f"quit loop now. PASSED={not failed}")
            # return int(failed) # temp
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
