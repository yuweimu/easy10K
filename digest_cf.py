"""Validate cash-flow statements after IncomeOK, or any report with --hash.

Run: python digest_cf.py [--hash PDF_HASH] [--dry-run]
Net cash flows may be zero or negative. Minority-interest '其中' rows are
included disclosures, not additional cash receipts or payments.
"""

from decimal import Decimal

from statement_digestion import Equation, check_statement, run_cli


REQUIRED_IDS = (81, 82, 83, 85, 86, 87)


def accounting_equations(state, result):
    values, equations = state.amounts, []
    for parent in (8101, 8102, 8201, 8202, 8301, 8302):
        if parent not in values:
            continue
        children = [(item_id, 1) for item_id in values if str(item_id).startswith(str(parent))
                    and len(str(item_id)) == len(str(parent)) + 2]
        equations.append(Equation("children_sum", parent, tuple(children)))
    for parent in (81, 82, 83):
        if parent in values:
            inflow, outflow = parent * 100 + 1, parent * 100 + 2
            equations.append(Equation("net_cash_flow", parent, ((inflow, 1), (outflow, -1)), (inflow, outflow)))
    if 85 in values:
        equations.append(Equation("net_increase_in_cash", 85, ((81, 1), (82, 1), (83, 1), (84, 1)), (81, 82, 83)))
    if 87 in values:
        equations.append(Equation("closing_cash", 87, ((86, 1), (85, 1)), (86, 85)))
    for item_id in (86, 87):
        if values.get(item_id) is not None and values[item_id] < 0:
            result["issues"].append(f"Item {item_id}: cash and cash equivalents balance cannot be negative")
    for child, parent in ((83010101, 830101), (83020301, 830203)):
        amount, total = values.get(child), values.get(parent)
        if amount is None or total is None or amount < 0 or total < 0:
            continue
        tolerance = max(Decimal(result["tolerance"]["absolute_statement_units"]),
                        Decimal(result["tolerance"]["relative"]) * max(amount, total))
        passed = amount <= total + tolerance
        result["checks"].append({"check": "included_detail", "item_id": child,
                                 "parent_id": parent, "passed": passed})
        if not passed:
            result["issues"].append(f"Item {child}: included minority-interest amount exceeds item {parent}")
    # Financial-sector net receipt/payment components can legitimately be negative.
    # Do not impose nonnegative signs on every cash-flow detail or subtotal.
    return equations


def check_report(row, chart, *, absolute_tolerance=Decimal("0.00001"), relative_tolerance=Decimal("0.00001")):
    return check_statement(row, chart, "cash_flow_statement", REQUIRED_IDS, accounting_equations,
                           absolute_tolerance=absolute_tolerance, relative_tolerance=relative_tolerance,
                           required_numeric_ids=(86, 87))


def main(argv=None):
    return run_cli("cash_flow_statement", check_report, argv)


if __name__ == "__main__":
    raise SystemExit(main())
