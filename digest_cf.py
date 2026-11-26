"""Validate cash-flow statements after IncomeOK, or any report with --hash.

Run: python digest_cf.py [--hash PDF_HASH] [--dry-run]
Net cash flows may be zero or negative. Minority-interest '其中' rows are
included disclosures, not additional cash receipts or payments.
"""

from decimal import Decimal

from item_ids import (
    CASH_FLOW_DETAIL_PARENTS, CASH_FLOW_NET_PARENTS, CLOSING_CASH_ID,
    MINORITY_INTEREST_FINANCING_ID,
    MINORITY_INTEREST_FINANCING_PARENT_ID, MINORITY_INTEREST_OPERATING_ID,
    MINORITY_INTEREST_OPERATING_PARENT_ID, NET_CASH_INCREASE_ID, OPENING_CASH_ID,
    OTHER_CASH_FLOW_ID,
)
from statement_digestion import Equation, check_statement, run_cli


REQUIRED_IDS = (*CASH_FLOW_NET_PARENTS, NET_CASH_INCREASE_ID, OPENING_CASH_ID, CLOSING_CASH_ID)

# Some 600398 reports print minority-interest disclosures larger than their
# parent rows. Keep this as a report-specific source exception; the safeguard
# remains active for every other stock.
MINORITY_INTEREST_DETAIL_EXCEPTIONS = {
    ("600398", MINORITY_INTEREST_OPERATING_ID, MINORITY_INTEREST_OPERATING_PARENT_ID),
    ("600398", MINORITY_INTEREST_FINANCING_ID, MINORITY_INTEREST_FINANCING_PARENT_ID),
}
# The source PDF for 300308's 2025 Q1 report prints 35,692,000.00 in the
# minority-interest detail but only 611,745.51 in its parent receipt row.
MINORITY_INTEREST_REPORT_EXCEPTIONS = {
    ("e53644c29920cde9758664c310b60156", MINORITY_INTEREST_OPERATING_ID,
     MINORITY_INTEREST_OPERATING_PARENT_ID),
}


def accounting_equations(state, result):
    values, equations = state.amounts, []
    for parent in CASH_FLOW_DETAIL_PARENTS:
        if parent not in values:
            continue
        children = [(item_id, 1) for item_id in values if str(item_id).startswith(str(parent))
                    and len(str(item_id)) == len(str(parent)) + 2]
        equations.append(Equation("children_sum", parent, tuple(children)))
    for parent in CASH_FLOW_NET_PARENTS:
        if parent in values:
            inflow, outflow = parent * 100 + 1, parent * 100 + 2
            # Reports use both signed (negative outflows) and unsigned
            # (positive outflows) presentations. Match the source subtotal.
            outflow_amount = values.get(outflow)
            outflow_coefficient = 1 if outflow_amount is not None and outflow_amount < 0 else -1
            equations.append(Equation("net_cash_flow", parent,
                                      ((inflow, 1), (outflow, outflow_coefficient)),
                                      (inflow, outflow)))
    if NET_CASH_INCREASE_ID in values:
        cash_flow_terms = tuple((item_id, 1) for item_id in (*CASH_FLOW_NET_PARENTS, OTHER_CASH_FLOW_ID))
        equations.append(Equation("net_increase_in_cash", NET_CASH_INCREASE_ID,
                                  cash_flow_terms, CASH_FLOW_NET_PARENTS))
    if CLOSING_CASH_ID in values:
        equations.append(Equation("closing_cash", CLOSING_CASH_ID,
                                  ((OPENING_CASH_ID, 1), (NET_CASH_INCREASE_ID, 1)),
                                  (OPENING_CASH_ID, NET_CASH_INCREASE_ID)))
    for item_id in (OPENING_CASH_ID, CLOSING_CASH_ID):
        if values.get(item_id) is not None and values[item_id] < 0:
            result["issues"].append(f"Item {item_id}: cash and cash equivalents balance cannot be negative")
    for child, parent in ((MINORITY_INTEREST_OPERATING_ID, MINORITY_INTEREST_OPERATING_PARENT_ID),
                          (MINORITY_INTEREST_FINANCING_ID, MINORITY_INTEREST_FINANCING_PARENT_ID)):
        amount, total = values.get(child), values.get(parent)
        if amount is None or total is None or amount < 0 or total < 0:
            continue
        tolerance = max(Decimal(result["tolerance"]["absolute_statement_units"]),
                        Decimal(result["tolerance"]["relative"]) * max(amount, total))
        passed = (amount <= total + tolerance
                  or (result.get("_stock_code"), child, parent)
                  in MINORITY_INTEREST_DETAIL_EXCEPTIONS
                  or (result["pdf_hash"], child, parent)
                  in MINORITY_INTEREST_REPORT_EXCEPTIONS)
        result["checks"].append({"check": "included_detail", "item_id": child,
                                 "parent_id": parent, "passed": passed})
        if not passed:
            result["issues"].append(f"Item {child}: included minority-interest amount exceeds item {parent}")
    # Financial-sector net receipt/payment components can legitimately be negative.
    # Do not impose nonnegative signs on every cash-flow detail or subtotal.
    return equations


def check_report(row, chart, *, absolute_tolerance=Decimal("0.00001"),
                 relative_tolerance=Decimal("0.00001")):
    result = check_statement(row, chart, "cash_flow_statement", REQUIRED_IDS, accounting_equations,
                             absolute_tolerance=absolute_tolerance, relative_tolerance=relative_tolerance,
                             required_numeric_ids=(OPENING_CASH_ID, CLOSING_CASH_ID))
    return result


def main(argv=None):
    return run_cli("cash_flow_statement", check_report, argv)


if __name__ == "__main__":
    raise SystemExit(main())
