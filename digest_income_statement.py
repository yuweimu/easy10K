"""Validate income statements after BalanceSheetOK, or any report with --hash.

Run: python digest_income_statement.py [--hash PDF_HASH] [--dry-run]
Required totals must be present; zero profits and losses are valid.
EPS is stored in yuan per share and is never scaled by the statement unit.
"""

from decimal import Decimal

from statement_digestion import Equation, check_statement, run_cli


REQUIRED_IDS = (51, 54, 56)


def accounting_equations(state, result):
    values, equations = state.amounts, []

    # Some reports omit the amount on OCI attribution rows while providing
    # amounts for their children. Derive only missing/blank parents; an
    # explicitly reported amount, including zero, remains authoritative.
    for parent in (5701, 5703):
        children = [item_id for item_id in values
                    if str(item_id).startswith(str(parent))
                    and len(str(item_id)) == len(str(parent)) + 2]
        if (parent not in values or values[parent] is None) and children:
            values[parent] = sum((values[item_id] or Decimal(0) for item_id in children), Decimal(0))

    def add(name, target, terms, required=()):
        if target in values:
            equations.append(Equation(name, target, tuple(terms), tuple(required)))

    for parent in (41, 42, 5701, 570101, 570103, 57, 58):
        children = [item_id for item_id in values if str(item_id).startswith(str(parent))
                    and len(str(item_id)) == len(str(parent)) + 2]
        if children:
            add("children_sum", parent, [(item_id, 1) for item_id in children])
    revenue = 41 if 41 in values else 4101
    costs = [(42, -1)] if 42 in values else [(item_id, -1) for item_id in values
                                             if str(item_id).startswith("42") and len(str(item_id)) == 4]
    if revenue not in values:
        result["issues"].append("Missing revenue: expected 营业总收入 or 营业收入")
    if not costs:
        result["issues"].append("Missing operating cost or expense items")
    adjustments = [(item_id, state.signs.get(item_id, -1 if item_id in (48, 49) else 1))
                   for item_id in range(43, 51)]
    add("operating_profit", 51, [(revenue, 1), *costs, *adjustments], [revenue])
    add("profit_before_tax", 54, [(51, 1), (52, 1), (53, -1)], [51])
    add("net_profit", 56, [(54, 1), (55, -1)], [54, 55])
    if 560101 in values or 560103 in values:
        add("net_profit_by_operations", 56, [(560101, 1), (560103, 1)])
    if 560201 in values or 560203 in values:
        add("net_profit_by_ownership", 56, [(560201, 1), (560203, 1)])
    add("comprehensive_income", 58, [(56, 1), (57, 1)], [56])
    for total, profit, other_income in ((5801, 560201, 5701), (5803, 560203, 5703)):
        if profit in values:
            add("comprehensive_income_by_ownership", total, [(profit, 1), (other_income, 1)], [profit])
    return equations


def check_report(row, chart, *, absolute_tolerance=Decimal("0.01"), relative_tolerance=Decimal("0.0001")):
    return check_statement(row, chart, "income_statement", REQUIRED_IDS, accounting_equations,
                           absolute_tolerance=absolute_tolerance, relative_tolerance=relative_tolerance,
                           required_numeric_ids=REQUIRED_IDS)


def main(argv=None):
    return run_cli("income_statement", check_report, argv)


if __name__ == "__main__":
    raise SystemExit(main())
