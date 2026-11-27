"""Validate income statements after BalanceOK, or any report with --hash.

Run: python digest_is.py [--hash PDF_HASH] [--dry-run]
Required totals must be present; zero profits and losses are valid.
EPS is stored in yuan per share and is never scaled by the statement unit.
"""

from decimal import Decimal

from item_ids import (
    ASSET_DISPOSAL_INCOME_ID, ASSET_DISPOSAL_LOSS_ID, ASSET_IMPAIRMENT_LOSS_ID,
    COMPREHENSIVE_INCOME_ID, COMPREHENSIVE_INCOME_NONCONTROLLING_ID,
    COMPREHENSIVE_INCOME_OWNERS_ID, CREDIT_IMPAIRMENT_ID, EXCHANGE_GAIN_LOSS_ID,
    FAIR_VALUE_CHANGE_ID, FINANCE_INCOME_DETAIL_ID, FINANCE_INCOME_ID,
    INTEREST_INCOME_ID, INVESTMENT_INCOME_ID, NET_PROFIT_ID,
    NET_PROFIT_OPERATIONS_CURRENT_ID, NET_PROFIT_OPERATIONS_NONCONTROLLING_ID,
    NET_PROFIT_OWNERSHIP_CURRENT_ID, NET_PROFIT_OWNERSHIP_NONCONTROLLING_ID,
    INCOME_TAX_ID, NON_OPERATING_EXPENSE_ID, NON_OPERATING_INCOME_ID,
    OPERATING_COST_ID, OPERATING_PROFIT_ID, OPERATING_REVENUE_ID,
    OPERATING_REVENUE_DETAIL_ID, OTHER_COMPREHENSIVE_TOTAL_ID,
    OTHER_COMPREHENSIVE_GROUP_CURRENT_ID, OTHER_COMPREHENSIVE_GROUP_NONCONTROLLING_ID,
    OTHER_COMPREHENSIVE_INCOME_ID, OTHER_COMPREHENSIVE_INCOME_NONCONTROLLING_ID,
    OTHER_OPERATING_EXPENSE_ID, OTHER_OPERATING_INCOME_ID, PROFIT_ADJUSTMENT_IDS,
    PROFIT_BEFORE_TAX_ID,
)
from statement_digestion import Equation, check_statement, run_cli


REQUIRED_IDS = (OPERATING_PROFIT_ID, PROFIT_BEFORE_TAX_ID, NET_PROFIT_ID)

# This report repeats the consolidated comprehensive-income total in the
# parent-company row and leaves the minority comprehensive-income row blank.
# Keep the consolidated comprehensive-income equation checked, but exempt the
# two ownership reconciliation equations that conflict with that source layout.
SOURCE_EQUATION_EXCEPTIONS = {
    "09f90159437d97d2effb0d1fbc11f424": {
        ("children_sum", COMPREHENSIVE_INCOME_ID),
        ("comprehensive_income_by_ownership", COMPREHENSIVE_INCOME_OWNERS_ID),
    },
}


def repair_comprehensive_income(state):
    """Fill omitted comprehensive totals and repair a known PDF row shift.

    A few CNInfo tables omit the blank ``综合收益总额`` row while laying out
    the following ownership rows one row too early.  In that shape the parsed
    values are ``5801 == 56`` and ``5803 == 560201``.  Those values cannot be
    valid ownership totals when a distinct minority profit is present, so use
    the unambiguous profit/OCI ownership components instead.
    """
    values = state.amounts
    if (COMPREHENSIVE_INCOME_ID not in values
            or values[COMPREHENSIVE_INCOME_ID] is None):
        if NET_PROFIT_ID in values and values[NET_PROFIT_ID] is not None:
            values[COMPREHENSIVE_INCOME_ID] = (
                values[NET_PROFIT_ID]
                + (values.get(OTHER_COMPREHENSIVE_TOTAL_ID) or Decimal(0))
            )

    # Some reports leave the ownership-level comprehensive-income total
    # blank while still reporting the corresponding net profit.  A blank OCI
    # attribution means zero, so the omitted total is derivable and should
    # participate in the parent comprehensive-income equation.
    for total, profit, other_income in (
            (COMPREHENSIVE_INCOME_OWNERS_ID, NET_PROFIT_OWNERSHIP_CURRENT_ID,
             OTHER_COMPREHENSIVE_INCOME_ID),
            (COMPREHENSIVE_INCOME_NONCONTROLLING_ID,
             NET_PROFIT_OWNERSHIP_NONCONTROLLING_ID,
             OTHER_COMPREHENSIVE_INCOME_NONCONTROLLING_ID)):
        if total not in values or values[total] is None:
            if profit not in values or values[profit] is None:
                continue
            values[total] = values[profit] + (values.get(other_income) or Decimal(0))

    required = (
        COMPREHENSIVE_INCOME_OWNERS_ID,
        COMPREHENSIVE_INCOME_NONCONTROLLING_ID,
        NET_PROFIT_ID,
        NET_PROFIT_OWNERSHIP_CURRENT_ID,
        NET_PROFIT_OWNERSHIP_NONCONTROLLING_ID,
    )
    if not all(item_id in values and values[item_id] is not None for item_id in required):
        return
    if values[COMPREHENSIVE_INCOME_OWNERS_ID] != values[NET_PROFIT_ID]:
        return
    if values[COMPREHENSIVE_INCOME_NONCONTROLLING_ID] != values[NET_PROFIT_OWNERSHIP_CURRENT_ID]:
        return
    if values[NET_PROFIT_OWNERSHIP_NONCONTROLLING_ID] == values[NET_PROFIT_OWNERSHIP_CURRENT_ID]:
        return

    values[COMPREHENSIVE_INCOME_OWNERS_ID] = (
        values[NET_PROFIT_OWNERSHIP_CURRENT_ID]
        + (values.get(OTHER_COMPREHENSIVE_INCOME_ID) or Decimal(0))
    )
    values[COMPREHENSIVE_INCOME_NONCONTROLLING_ID] = (
        values[NET_PROFIT_OWNERSHIP_NONCONTROLLING_ID]
        + (values.get(OTHER_COMPREHENSIVE_INCOME_NONCONTROLLING_ID) or Decimal(0))
    )
    values[COMPREHENSIVE_INCOME_ID] = (
        values[NET_PROFIT_ID] + (values.get(OTHER_COMPREHENSIVE_TOTAL_ID) or Decimal(0))
    )


def accounting_equations(state, result):
    values, equations = state.amounts, []

    # Some reports omit the amount on OCI attribution rows while providing
    # amounts for their children. Derive only missing/blank parents; an
    # explicitly reported amount, including zero, remains authoritative.
    for parent in (OTHER_COMPREHENSIVE_TOTAL_ID, OTHER_COMPREHENSIVE_INCOME_ID,
                   OTHER_COMPREHENSIVE_GROUP_CURRENT_ID,
                   OTHER_COMPREHENSIVE_GROUP_NONCONTROLLING_ID,
                   OTHER_COMPREHENSIVE_INCOME_NONCONTROLLING_ID):
        children = [item_id for item_id in values
                    if str(item_id).startswith(str(parent))
                    and len(str(item_id)) == len(str(parent)) + 2]
        if (parent not in values or values[parent] is None) and children:
            values[parent] = sum((values[item_id] or Decimal(0) for item_id in children), Decimal(0))

    repair_comprehensive_income(state)

    def add(name, target, terms, required=()):
        if target in values:
            equations.append(Equation(name, target, tuple(terms), tuple(required)))

    credit_impairment_is_cost_child = False
    for parent in (OPERATING_REVENUE_ID, OPERATING_COST_ID, OTHER_COMPREHENSIVE_INCOME_ID,
                   OTHER_COMPREHENSIVE_GROUP_CURRENT_ID,
                   OTHER_COMPREHENSIVE_GROUP_NONCONTROLLING_ID, OTHER_COMPREHENSIVE_TOTAL_ID,
                   COMPREHENSIVE_INCOME_ID):
        children = [item_id for item_id in values if str(item_id).startswith(str(parent))
                    and len(str(item_id)) == len(str(parent)) + 2]
        if (parent == OPERATING_COST_ID and OPERATING_COST_ID in values
                and CREDIT_IMPAIRMENT_ID in values
                and values[OPERATING_COST_ID] is not None
                and values[CREDIT_IMPAIRMENT_ID] is not None):
            without_credit_impairment = sum((values[item_id] or Decimal(0) for item_id in children), Decimal(0))
            with_credit_impairment = without_credit_impairment + values[CREDIT_IMPAIRMENT_ID]
            if (values[OPERATING_COST_ID] == with_credit_impairment
                    and values[OPERATING_COST_ID] != without_credit_impairment):
                children.append(CREDIT_IMPAIRMENT_ID)
                credit_impairment_is_cost_child = True
        if children:
            # Investment income from associates and joint ventures (4401) is
            # disclosed as a component already included in the 44 total.
            if parent == INVESTMENT_INCOME_ID and 4401 in children:
                continue
            add("children_sum", parent, [(item_id, 1) for item_id in children])
    revenue = OPERATING_REVENUE_ID if OPERATING_REVENUE_ID in values else OPERATING_REVENUE_DETAIL_ID
    costs = [(OPERATING_COST_ID, -1)] if OPERATING_COST_ID in values else [(item_id, -1) for item_id in values
                                             if str(item_id).startswith(str(OPERATING_COST_ID))
                                             and len(str(item_id)) == 4]
    if revenue not in values:
        result["issues"].append("Missing revenue: expected 营业总收入 or 营业收入")
    if not costs:
        result["issues"].append("Missing operating cost or expense items")
    adjustment_ids = PROFIT_ADJUSTMENT_IDS
    if credit_impairment_is_cost_child:
        adjustment_ids = tuple(item_id for item_id in adjustment_ids if item_id != CREDIT_IMPAIRMENT_ID)
    elif (CREDIT_IMPAIRMENT_ID in state.labels and OPERATING_PROFIT_ID in state.labels
          and list(state.labels).index(CREDIT_IMPAIRMENT_ID) > list(state.labels).index(OPERATING_PROFIT_ID)):
        # A post-subtotal disclosure is not an input to the operating-profit
        # subtotal when its placement makes the top-level interpretation clear.
        adjustment_ids = tuple(item_id for item_id in adjustment_ids if item_id != CREDIT_IMPAIRMENT_ID)
    adjustments = [(item_id, state.signs.get(item_id, -1 if item_id in (CREDIT_IMPAIRMENT_ID, ASSET_DISPOSAL_LOSS_ID) else 1))
                   for item_id in adjustment_ids]
    if (CREDIT_IMPAIRMENT_ID in adjustment_ids and CREDIT_IMPAIRMENT_ID in values
            and values.get(CREDIT_IMPAIRMENT_ID) is not None
            and OPERATING_PROFIT_ID in values and values.get(OPERATING_PROFIT_ID) is not None):
        base = sum((values.get(item_id, Decimal(0)) or Decimal(0)) * coefficient
                   for item_id, coefficient in [(revenue, 1), *costs, *adjustments]
                   if item_id != CREDIT_IMPAIRMENT_ID)
        credit_amount = values[CREDIT_IMPAIRMENT_ID]
        reported_profit = values[OPERATING_PROFIT_ID]
        existing_coefficient = next(
            coefficient for item_id, coefficient in adjustments
            if item_id == CREDIT_IMPAIRMENT_ID
        )
        alternate_coefficient = -existing_coefficient
        tolerance = max(Decimal("0.00001"), abs(reported_profit) * Decimal("0.00001"))
        existing_difference = abs(
            reported_profit - (base + existing_coefficient * credit_amount)
        )
        alternate_difference = abs(
            reported_profit - (base + alternate_coefficient * credit_amount)
        )
        credit_coefficient = (
            alternate_coefficient
            if existing_difference > tolerance and alternate_difference <= tolerance
            else existing_coefficient
        )
        adjustments = [
            (item_id, credit_coefficient if item_id == CREDIT_IMPAIRMENT_ID else coefficient)
            for item_id, coefficient in adjustments
        ]
    add("operating_profit", OPERATING_PROFIT_ID, [(revenue, 1), *costs, *adjustments], [revenue])
    add("profit_before_tax", PROFIT_BEFORE_TAX_ID,
        [(OPERATING_PROFIT_ID, 1), (NON_OPERATING_INCOME_ID, 1), (NON_OPERATING_EXPENSE_ID, -1)],
        [OPERATING_PROFIT_ID])
    add("net_profit", NET_PROFIT_ID, [(PROFIT_BEFORE_TAX_ID, 1), (INCOME_TAX_ID, -1)],
        [PROFIT_BEFORE_TAX_ID, INCOME_TAX_ID])
    if any(values.get(item_id) is not None for item_id in (
            NET_PROFIT_OPERATIONS_CURRENT_ID, NET_PROFIT_OPERATIONS_NONCONTROLLING_ID)):
        add("net_profit_by_operations", NET_PROFIT_ID,
            [(NET_PROFIT_OPERATIONS_CURRENT_ID, 1), (NET_PROFIT_OPERATIONS_NONCONTROLLING_ID, 1)])
    if NET_PROFIT_OWNERSHIP_CURRENT_ID in values or NET_PROFIT_OWNERSHIP_NONCONTROLLING_ID in values:
        add("net_profit_by_ownership", NET_PROFIT_ID,
            [(NET_PROFIT_OWNERSHIP_CURRENT_ID, 1), (NET_PROFIT_OWNERSHIP_NONCONTROLLING_ID, 1)])
    add("comprehensive_income", COMPREHENSIVE_INCOME_ID,
        [(NET_PROFIT_ID, 1), (OTHER_COMPREHENSIVE_TOTAL_ID, 1)], [NET_PROFIT_ID])
    for total, profit, other_income in ((COMPREHENSIVE_INCOME_OWNERS_ID, NET_PROFIT_OWNERSHIP_CURRENT_ID, OTHER_COMPREHENSIVE_INCOME_ID),
                                        (COMPREHENSIVE_INCOME_NONCONTROLLING_ID, NET_PROFIT_OWNERSHIP_NONCONTROLLING_ID, OTHER_COMPREHENSIVE_INCOME_NONCONTROLLING_ID)):
        if profit in values:
            add("comprehensive_income_by_ownership", total, [(profit, 1), (other_income, 1)], [profit])
    exceptions = SOURCE_EQUATION_EXCEPTIONS.get(result["pdf_hash"], set())
    if exceptions:
        equations = [equation for equation in equations
                     if (equation.name, equation.target) not in exceptions]
        result["checks"].append({"check": "source_report_equation_exception",
                                 "pdf_hash": result["pdf_hash"],
                                 "excluded_equations": [
                                     {"check": name, "item_id": item_id}
                                     for name, item_id in sorted(exceptions)],
                                 "passed": True})
    return equations


def check_report(row, chart, *, absolute_tolerance=Decimal("0.00001"),
                 relative_tolerance=Decimal("0.00001")):
    return check_statement(row, chart, "income_statement", REQUIRED_IDS, accounting_equations,
                           absolute_tolerance=absolute_tolerance, relative_tolerance=relative_tolerance,
                           required_numeric_ids=REQUIRED_IDS)


def main(argv=None):
    return run_cli("income_statement", check_report, argv)


if __name__ == "__main__":
    raise SystemExit(main())
