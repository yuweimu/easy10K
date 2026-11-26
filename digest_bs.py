"""Validate and digest the first amount column of AuditOK balance sheets.

Run: python digest_bs.py [--hash PDF_HASH] [--retry]
Amounts are checked in statement units, then converted to yuan for storage.
The default tolerance is the greater of 0.01 statement units and 0.1 percent.
"""

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
import re

from line_item_chart import LineItem, LineItemChart
from digest_cli import print_equation, run_digest_cli
from item_ids import (
    ASSETS_ID, CAPITAL_RESERVE_ID, CASH_ID, CURRENT_ASSETS_ID,
    CURRENT_ASSETS_OTHER_DETAIL_ID, CURRENT_ASSETS_TOTAL_DETAIL_ID,
    CURRENT_ASSETS_TOTAL_ID, CURRENT_ASSETS_AMBIGUOUS_IDS,
    CURRENT_ASSETS_LOANS_ID, NONCURRENT_ASSETS_LOANS_ID,
    CURRENT_LIABILITIES_AMBIGUOUS_IDS, EQUITY_ID, EQUITY_TOTAL_ID,
    CURRENT_LIABILITIES_ID, LIABILITIES_ID, OTHER_EQUITY_DETAIL_ID,
    OTHER_RECEIVABLES_ID, OTHER_EQUITY_TOTAL_ID,
    RECEIVABLE_COMPONENT_IDS, RECEIVABLES_TOTAL_ID, RECEIVABLE_ACCOUNTS_ID,
    RECEIVABLE_NOTES_ID, RETAINED_EARNINGS_ID, ROOT_ITEM_ID, SHARE_CAPITAL_ID,
    TREASURY_STOCK_ID, UNMAPPED_ITEM_ID,
)
from line_item_storage import LineItemStorage
from statement_parsing import split_cells


REQUIRED_IDS = (ASSETS_ID, CURRENT_ASSETS_ID, CASH_ID, OTHER_RECEIVABLES_ID,
                CURRENT_ASSETS_TOTAL_ID, CURRENT_ASSETS_TOTAL_DETAIL_ID,
                CURRENT_ASSETS_OTHER_DETAIL_ID, LIABILITIES_ID,
                CURRENT_LIABILITIES_ID, EQUITY_ID, EQUITY_TOTAL_ID,
                SHARE_CAPITAL_ID, CAPITAL_RESERVE_ID, OTHER_EQUITY_DETAIL_ID)
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
    units = set(re.findall(
        r"(?:金额)?单位[：:（(]?(?:均为|为)?(?:人民币)?(百万元|亿元|万元|千元|元)",
        compact,
    ))
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
    # Match Unicode suffixes of long names and aliases after label normalization.
    long_names = [clean_cell(name).replace("(", "（").replace(")", "）")
                  for name in chart.items if len(clean_cell(name)) >= 16]
    parsed, ambiguous_rows, shifted_items = state.items, state.ambiguous_rows, state.shifted_items
    issues = state.issues
    def normalize_balance_label(text):
        label = clean_cell(re.sub(r"【[^】]*(?:】|$)", "", text)).replace("(", "（").replace(")", "）")
        label = re.sub(r"^[☆★]+", "", label)
        label = re.sub(r"^(?:[一二三四五六七八九十\d]+[、.．]|[（(][一二三四五六七八九十\d]+[）)])", "", label)
        return label.rstrip("：:")

    lines = markdown.splitlines()
    table_width = None
    asset_section = None
    for index, line in enumerate(lines):
        line_number = index + 1
        if not line.strip().startswith("|"):
            continue
        cells = split_cells(line)
        if len(cells) < 2:
            continue
        label = clean_cell(re.sub(r"【[^】]*(?:】|$)", "", cells[0])).replace("(", "（").replace(")", "）")
        label = re.sub(r"^[☆★]+", "", label)
        notes_header = (
            len(cells) >= 4
            and re.search(r"附注|注释|行次|^注$", clean_cell(cells[1]))
            and re.search(r"期末|期初|上年年末", clean_cell(cells[2]))
        )
        if label in ("项目", "科目") or notes_header:
            table_width = len(cells)
            amount_column = 2 if re.search(r"附注|注释|行次|^注$", clean_cell(cells[1])) else 1
            continue
        if table_width is not None and len(cells) != table_width:
            issues.append(f"Line {line_number}: expected {table_width} table columns, got {len(cells)}")
            continue
        if not label or re.fullmatch(r"[:\-]+", label):
            continue
        label = re.sub(r"^(?:[一二三四五六七八九十\d]+[、.．]|[（(][一二三四五六七八九十\d]+[）)])", "", label)
        section_heading = label.rstrip().endswith(("：", ":"))
        label = label.rstrip("：:")
        if label.startswith("流动资产") and "合计" not in label:
            asset_section = "current"
        elif label.startswith("非流动资产") and "合计" not in label:
            asset_section = "noncurrent"
        if section_heading and "合计" not in label and label.startswith((
                "流动资产", "非流动资产", "流动负债", "非流动负债", "所有者权益",
        )):
            continue
        item = chart.get(label)
        if label in ("发放贷款和垫款", "发放贷款及垫款"):
            item = chart._items_by_id.get(
                CURRENT_ASSETS_LOANS_ID if asset_section == "current"
                else NONCURRENT_ASSETS_LOANS_ID
            )
        if item is None or item.item_id in (ROOT_ITEM_ID, UNMAPPED_ITEM_ID):
            continue_if_total = re.search(r"负债[和及].*权益.*总计", label)
            truncated_suffix = len(label) >= 3 and any(
                len(name) > len(label) and name.endswith(label) for name in long_names)
            if item is None and not continue_if_total and not truncated_suffix:
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


def has_nonzero_receivables_subtotal(amounts: dict[int, Decimal]) -> bool:
    return all(item_id in amounts and amounts[item_id] != 0
               for item_id in (RECEIVABLES_TOTAL_ID, *RECEIVABLE_COMPONENT_IDS))


def subtotal_children(parent_id: int, amounts: dict[int, Decimal],
                      reporting_date: str | date | None, *,
                      present_items: set[int] | None = None) -> list[int]:
    """Select subtotal children using the hierarchy for the report's period."""
    if parent_id == RECEIVABLES_TOTAL_ID:
        if not has_nonzero_receivables_subtotal(amounts):
            return []
        return [item_id for item_id in RECEIVABLE_COMPONENT_IDS if item_id in amounts]
    prefix = str(parent_id)
    if len(prefix) not in (2, 4):
        return []
    children = [item_id for item_id in amounts
                if str(item_id).startswith(prefix) and len(str(item_id)) == len(prefix) + 2]
    if parent_id == CURRENT_ASSETS_ID and RECEIVABLES_TOTAL_ID in (
            present_items if present_items is not None else amounts):
        children = [item_id for item_id in children if item_id not in RECEIVABLE_COMPONENT_IDS]
    return children


def subtotal_child_options(parent_id: int, amounts: dict[int, Decimal],
                           reporting_date: str | date | None, present_items: set[int]) -> list[list[int]]:
    """Try both placements of interest/dividends receivable or payable."""
    children = subtotal_children(parent_id, amounts, reporting_date, present_items=present_items)
    options = [children]
    ambiguous_items = {CURRENT_ASSETS_ID: CURRENT_ASSETS_AMBIGUOUS_IDS,
                       CURRENT_LIABILITIES_ID: CURRENT_LIABILITIES_AMBIGUOUS_IDS}
    additional_children = [item_id for item_id in ambiguous_items.get(parent_id, ()) if item_id in amounts]
    if additional_children:
        options.append(children + additional_children)
    return options


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
        options = subtotal_child_options(parent_id, amounts, reporting_date, set(parsed))
        totals = [sum((-abs(amounts[item_id]) if item_id == TREASURY_STOCK_ID else amounts[item_id]
                       for item_id in children), Decimal(0)) for children in options]
        if any(abs(parent_amount - total) <= max(absolute_tolerance,
               relative_tolerance * max(abs(parent_amount), abs(total))) for total in totals):
            continue
        alternatives = {}
        for children, total in zip(options, totals):
            if not (shifted_items & (set(children) | {parent_id})):
                continue
            for item_id in children:
                contribution = -abs(amounts[item_id]) if item_id == TREASURY_STOCK_ID else amounts[item_id]
                if item_id not in ambiguous_rows or contribution == 0:
                    continue
                alternative = total - contribution
                tolerance = max(absolute_tolerance, relative_tolerance * max(abs(parent_amount), abs(alternative)))
                if abs(parent_amount - alternative) <= tolerance:
                    alternatives[item_id] = (alternative, total)
        if len(alternatives) == 1:
            item_id, (alternative, total) = next(iter(alternatives.items()))
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
    if RECEIVABLES_TOTAL_ID in parsed and RECEIVABLES_TOTAL_ID not in amounts:
        issues.append(f"Item {RECEIVABLES_TOTAL_ID}: missing amount for 应收票据及应收账款")
    for parent_id, parent_amount in amounts.items():
        if parent_id == RECEIVABLES_TOTAL_ID and not has_nonzero_receivables_subtotal(amounts):
            continue
        options = subtotal_child_options(parent_id, amounts, row.get("reporting_date"), set(parsed))
        def option_difference(children):
            total = sum((-abs(amounts[item_id]) if item_id == TREASURY_STOCK_ID else amounts[item_id]
                         for item_id in children), Decimal(0))
            tolerance = max(absolute_tolerance, relative_tolerance * max(abs(parent_amount), abs(total)))
            difference = abs(parent_amount - total)
            return (difference > tolerance, difference)
        children = min(options, key=option_difference)
        if not children and parent_id != RECEIVABLES_TOTAL_ID:
            continue
        child_amounts = [-abs(amounts[item_id]) if item_id == TREASURY_STOCK_ID else amounts[item_id]
                         for item_id in children]
        total = sum(child_amounts, Decimal(0))
        difference = abs(parent_amount - total)
        tolerance = max(absolute_tolerance, relative_tolerance * max(abs(parent_amount), abs(total)))
        passed = difference <= tolerance
        expression = " + ".join(
            (f"({amount})" if amount < 0 else str(amount)) + f"({parsed[item_id][0].name}, {item_id})"
            for item_id, amount in zip(children, child_amounts))
        print_equation(
            row["pdf_hash"], parent_id, parsed[parent_id][0].name,
            parent_amount, expression, total, difference, tolerance, passed,
        )
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
                 relative_tolerance: Decimal = Decimal("0.0001"),
                 emit_equations: bool = False) -> dict:
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


def main(argv: list[str] | None = None) -> int:
    def load_rows(storage, args):
        options = {"pdf_hash": args.pdf_hash, "retry": args.retry}
        if args.ex:
            options["ex"] = True
        if args.stock_code:
            options["stock_code"] = args.stock_code
        return storage.get_audit_pass_reports(**options)

    def save_row(storage, row, result, args, retry_failure=False):
        options = {"allow_any_stage": True} if args.pdf_hash or (args.retry and not retry_failure) else {}
        if args.ex:
            options["ex"] = True
        storage.store_line_item_digestion(row, result, **options)

    return run_digest_cli(
        "balance_sheet", check_report, load_rows, save_row, argv,
        description=__doc__, absolute_tolerance=Decimal("0.00001"),
        relative_tolerance=Decimal("0.00001"),
        retry_help="Retry reports at the BalanceErr stage",
        allow_dry_run=False,
        storage_class=LineItemStorage,
    )


if __name__ == "__main__":
    raise SystemExit(main())
