"""Shared parsing, signed accounting equations, and CLI for statement digestion."""

from dataclasses import dataclass, field
from decimal import Decimal
from difflib import SequenceMatcher
import re

from digest_bs import check_report_metadata, clean_cell, parse_amount, select_amount_column
from digest_cli import print_equation, run_digest_cli
from item_ids import (
    ASSET_DISPOSAL_INCOME_ID, ASSET_IMPAIRMENT_LOSS_ID, CREDIT_IMPAIRMENT_ID,
    FAIR_VALUE_CHANGE_ID, INVESTMENT_INCOME_ID, OTHER_COMPREHENSIVE_GROUP_CURRENT_ID,
    OTHER_COMPREHENSIVE_GROUP_NONCONTROLLING_ID, OTHER_COMPREHENSIVE_INCOME_ID,
    OTHER_COMPREHENSIVE_TOTAL_ID,
    NET_PROFIT_OWNERSHIP_CURRENT_ID, OPERATING_COST_ID,
    COMPREHENSIVE_INCOME_OWNERS_ID, INTEREST_INCOME_ID, FINANCE_INCOME_ID,
    FINANCE_INCOME_DETAIL_ID, ASSET_DISPOSAL_LOSS_ID,
    OTHER_OPERATING_INCOME_ID,
    EPS_ID, EPS_DILUTED_ID, PER_SHARE_ITEMS_ID,
    OPERATING_REVENUE_ID, COMPREHENSIVE_INCOME_ID, OPERATING_PROFIT_ID,
    OPERATING_REVENUE_DETAIL_ID, CASH_FLOW_NET_PARENTS, CLOSING_CASH_ID,
    INVESTING_CASH_FLOW_ID, FINANCING_CASH_FLOW_ID, OPENING_CASH_ID,
    NET_CASH_INCREASE_ID, NET_PROFIT_OPERATIONS_ID, NET_PROFIT_OWNERSHIP_ID,
)
from line_item_chart import LineItem, LineItemChart
from line_item_storage import LineItemStorage
from statement_parsing import split_cells


@dataclass
class ParsedStatement:
    amounts: dict[int, Decimal | None] = field(default_factory=dict)
    labels: dict[int, str] = field(default_factory=dict)
    fuzzy_rows: dict[int, dict] = field(default_factory=dict)
    ambiguous_rows: dict[int, int] = field(default_factory=dict)
    shifted_items: set[int] = field(default_factory=set)
    signs: dict[int, int] = field(default_factory=dict)
    issues: list[str] = field(default_factory=list)
    unmapped_rows: list[dict] = field(default_factory=list)


@dataclass(frozen=True)
class Equation:
    name: str
    target: int
    terms: tuple[tuple[int, int], ...]
    required_terms: tuple[int, ...] = ()


def normalize_label(text):
    label = clean_cell(re.sub(r"【[^】]*(?:】|$)", "", text)).replace("(", "（").replace(")", "）")
    label = re.sub(r"^[☆★]+", "", label)
    label = re.sub(r"^(?:[一二三四五六七八九十\d]+[、.．]|[（(][一二三四五六七八九十\d]+[）)])", "", label)
    # Some PDF table extractors retain the punctuation from a numbered row
    # while dropping its numeral (for example ``.持续经营净利润``).
    label = re.sub(r"^[=.．、]+", "", label)
    label = re.sub(r"^(?:其中|加|减)[：:]", "", label)
    label = re.sub(r"（减少）", "", label)
    label = re.sub(r"（[^）]*(?:净亏损|亏损总额|亏损|损失)[^）]*）$", "", label)
    label = re.sub(r"（[^）]*(?:净亏损|亏损总额|亏损|损失)[^）]*$", "", label)
    label = re.sub(r"[/／]?[（(](?:亏|损)$", "", label)
    label = re.sub(r"[/／]+$", "", label)
    label = re.sub(r"[/／]+(?=额)", "", label)
    label = re.sub(r"（损失）[/／]收益$", "收益", label)
    label = re.sub(r"（(?:人民币)?元(?:[/／╱]股)?）", "", label)
    label = re.sub(r"(现金流入小计|现金流出小计).*$", r"\1", label)
    label = (label.replace("其它", "其他").replace("国定资产", "固定资产")
             .replace("设定收益计划", "设定受益计划")
             .replace("其他综合亏损", "其他综合收益的税后净额")
             .replace("其他综合（亏损）/收益", "其他综合收益")
             .replace("其他综合（亏损）／收益", "其他综合收益"))
    label = re.sub(r"归属于母公司股东的其他综合收益(?:的税后净额)?",
                   "归属母公司所有者的其他综合收益的税后净额", label)
    return label.rstrip("：:")


def parse_statement(markdown, chart, statement):
    state = ParsedStatement()
    allowed = lambda item_id: (int(str(OPERATING_REVENUE_ID)[:2])
                               <= int(str(item_id)[:2]) <= int(str(PER_SHARE_ITEMS_ID)[:2])
                               if statement == "income_statement" else
                               int(str(CASH_FLOW_NET_PARENTS[0])[:2])
                               <= int(str(item_id)[:2]) <= int(str(CLOSING_CASH_ID)[:2]))
    names = {name: item for name, item in chart.items.items() if allowed(item.item_id)}

    def find_item(label, fuzzy=True):
        normalized_label = normalize_label(label)
        matching_label = re.sub(r"（损失）[/／]转回$", "损失", normalized_label)
        item = next((item for name, item in names.items()
                     if normalize_label(name) == matching_label), None)
        if item is not None:
            return item, False, 1.0
        if item is None and len(label) >= 8:
            item = next((item for name, item in names.items()
                         if normalize_label(name).startswith(matching_label)), None)
        if item is not None:
            return item, False, 1.0
        if fuzzy and len(matching_label) >= 4:
            scores = {}
            candidates = [(name, item) for name, item in names.items()]
            candidates.extend(
                (alias, chart._items_by_id[item_id])
                for alias, item_id in aliases.items()
                if item_id in chart._items_by_id and allowed(item_id)
            )
            for name, candidate in candidates:
                normalized_name = normalize_label(name)
                if len(normalized_name) < 4:
                    continue
                score = SequenceMatcher(None, matching_label, normalized_name).ratio()
                previous = scores.get(candidate.item_id)
                if previous is None or score > previous[0]:
                    scores[candidate.item_id] = (score, candidate)
            ranked = sorted(scores.values(), key=lambda match: match[0], reverse=True)
            if (ranked and ranked[0][0] >= 0.70
                    and (len(ranked) == 1 or ranked[0][0] - ranked[1][0] >= 0.12)):
                return ranked[0][1], True, ranked[0][0]
        return None, False, None

    aliases = {"归属于母公司所有者的净利润": NET_PROFIT_OWNERSHIP_CURRENT_ID,
               "营业税金及附加": 4217,
               "归属于母公司所有者的其他综合收益的税后净额": OTHER_COMPREHENSIVE_INCOME_ID,
               "归属于母公司股东的其他综合收益的税后净额": OTHER_COMPREHENSIVE_INCOME_ID,
               "归属于母公司股东的综合收益总额": COMPREHENSIVE_INCOME_OWNERS_ID,
               "重新计算设定受益计划的变动额": 57010101,
               "应收款项融资信用减值准备": 57010307,
               "套期成本-远期要素": 57010309,
               "其他综合收益的税后净额小计": OTHER_COMPREHENSIVE_TOTAL_ID,
               "资产处置（损失）/收益": ASSET_DISPOSAL_INCOME_ID,
               "公允价值变动损益": FAIR_VALUE_CHANGE_ID,
               "信用减值转回": CREDIT_IMPAIRMENT_ID,
               "投资活动使用的现金流量净额": INVESTING_CASH_FLOW_ID,
               "筹资活动（使用）/产生的现金流量净额": FINANCING_CASH_FLOW_ID,
               "筹资活动产生/（使用）的现金流量净额": FINANCING_CASH_FLOW_ID,
               "筹资活动产生/(使用)的现金流量净额": FINANCING_CASH_FLOW_ID,
               "筹资活动使用的现金流量净额": FINANCING_CASH_FLOW_ID,
               "筹资活动使用现金流量净额": FINANCING_CASH_FLOW_ID,
               "黄金租赁业务收到的现金": 830106,
               "发行债券和短期融资券收到的现金": 830104,
               "发行短期融资券收到的现金": 830104,
               "发行可转换债券收到的现金": 830104,
               "偿还债券支付的现金": 830210,
               "偿还超短期融资券支付的现金": 830210,
               "偿还黄金租赁业务支付的现金": 830208,
               "偿还债券和超短期融资券支付的现金": 830210,
               "年初现金及现金等价物余额": OPENING_CASH_ID,
               "年末现金及现金等价物余额": CLOSING_CASH_ID,
               "现金及现金等价物净/增加额": NET_CASH_INCREASE_ID,
               "投资损失": INVESTMENT_INCOME_ID, "公允价值变动损失": FAIR_VALUE_CHANGE_ID,
               "资产处置损失": ASSET_DISPOSAL_INCOME_ID}
    amount_column, finance_details, oci_group = 1, False, None
    in_operating_costs = False
    previous_label = previous_full_name = ""
    pending_label = ""
    table_width = None

    def statement_amount_column(cells):
        if statement == "income_statement" and len(cells) == 5:
            return 3
        return select_amount_column(cells, amount_column)

    def dynamic_other_item(parent_id):
        """Register the non-zero ``其他`` child for a statement parent."""
        item_id = parent_id * 100 + 99
        parent = chart._items_by_id[parent_id]
        name = f"{parent.name}之其他项"
        item = chart._items_by_id.get(item_id)
        if item is None:
            item = LineItem(name, item_id, f"Other item under {parent.english_name}")
            chart._items_by_id[item_id] = item
        chart.items[name] = item
        chart.item_ids[name] = item_id
        return item_id

    lines = markdown.splitlines()
    for index, line in enumerate(lines):
        number = index + 1
        if not line.strip().startswith("|"):
            continue
        cells = split_cells(line)
        if len(cells) < 2:
            continue
        raw = clean_cell(cells[0])
        label = normalize_label(cells[0])
        if pending_label:
            label = normalize_label(pending_label + label)
            raw = pending_label + raw
            pending_label = ""
        if label in ("项目", "科目"):
            table_width = len(cells)
            amount_column = 2 if re.search(r"附注|注释|行次|^注$", clean_cell(cells[1])) else 1
            continue

        if table_width is not None and len(cells) != table_width:
            # use strict check temperatorily. Disable it if too strict. 
            state.issues.append(
                f"Line {number}: expected {table_width} table columns, got {len(cells)}"
            )
            continue
        if not label or re.fullmatch(r"[:\-]+", label):
            continue
        if (re.search(r"[/／]?[（(](?:亏|损)$", raw)
                and not any(clean_cell(cell) for cell in cells[1:])):
            pending_label = raw
            continue
        if (len(label) >= 8 and not label.endswith("产生的现金流量")
                and not any(clean_cell(cell) for cell in cells[1:])
                and any(normalize_label(name).startswith(label)
                        and len(normalize_label(name)) > len(label)
                        for name in names)):
            pending_label = raw
            continue
        if (statement == "cash_flow_statement"
                and not label.endswith("产生的现金流量")
                and not any(clean_cell(cell) for cell in cells[1:])
                and any(normalize_label(name).startswith(label)
                        and len(normalize_label(name)) > len(label)
                        for name in names)):
            pending_label = label
            continue
        # Section headings such as "投资活动产生的现金流量：" are prefixes
        # of the corresponding "...现金流量净额" item, but are not rows with
        # an amount. Do not let prefix matching turn their placeholder into 0.
        item, fuzzy_match, fuzzy_score = ((None, False, None)
                                          if label.endswith("产生的现金流量")
                                          else find_item(label, fuzzy=label not in aliases))
        item_id = item.item_id if item else aliases.get(label)
        if statement == "income_statement":
            if label == "资产减值损失" and in_operating_costs:
                item_id = ASSET_IMPAIRMENT_LOSS_ID
            if label == "利息收入" and finance_details:
                item_id = INTEREST_INCOME_ID
            elif label == "其他" and oci_group in (OTHER_COMPREHENSIVE_GROUP_CURRENT_ID,
                                                    OTHER_COMPREHENSIVE_GROUP_NONCONTROLLING_ID):
                column = statement_amount_column(cells)
                amount_text = clean_cell(cells[column]) if len(cells) > column else ""
                try:
                    amount = parse_amount(amount_text) if amount_text else None
                except ValueError:
                    amount = None
                if amount is not None and amount != 0:
                    item_id = dynamic_other_item(oci_group)
            if item_id in (FINANCE_INCOME_ID, FINANCE_INCOME_DETAIL_ID, INTEREST_INCOME_ID):
                finance_details = True
            elif item_id is not None:
                finance_details = False
            if item_id in (OTHER_COMPREHENSIVE_GROUP_CURRENT_ID,
                           OTHER_COMPREHENSIVE_GROUP_NONCONTROLLING_ID):
                oci_group = item_id
            in_operating_costs = item_id is not None and (
                item_id == OPERATING_COST_ID or str(item_id).startswith(str(OPERATING_COST_ID))
            )
        if item_id is None:
            continuation = (previous_full_name.startswith(previous_label + label)
                            and not any(clean_cell(cell) for cell in cells[1:]))
            suffix = len(label) >= 3 and any(len(name) >= 16 and len(name) > len(label) and name.endswith(label) for name in names)
            if not continuation and not suffix:
                column = statement_amount_column(cells)
                amount_text = clean_cell(cells[column]) if len(cells) > column else ""
                if not amount_text:
                    continue
                try:
                    amount = parse_amount(amount_text)
                except ValueError:
                    amount = None
                    amount_is_valid = False
                else:
                    amount_is_valid = True
                if amount_is_valid and (amount is None or amount == 0):
                    continue
                state.unmapped_rows.append({"line": number, "label": label})
            continue
        previous_label, previous_full_name = label, normalize_label(chart._items_by_id[item_id].name)
        column = statement_amount_column(cells)
        if len(cells) <= column:
            state.issues.append(f"Line {number}: missing current amount for {label}")
            continue
        try:
            amount = parse_amount(cells[column])
        except ValueError as error:
            if item_id in (EPS_ID, EPS_DILUTED_ID) and clean_cell(cells[column]) in {"不适用", "不适用。"}:
                amount = None
            else:
                state.issues.append(f"Line {number}: {label}: {error}")
                continue
        if item_id in state.amounts and state.amounts[item_id] is not None:
            if amount is not None and amount != state.amounts[item_id]:
                aggregate_rows = {
                    830104: {
                        "发行债券收到的现金",
                        "发行债券和短期融资券收到的现金",
                        "发行短期融资券收到的现金",
                        "发行可转换债券收到的现金",
                    },
                    830210: {
                        "偿还债券支付的现金",
                        "偿还超短期融资券支付的现金",
                        "偿还债券和超短期融资券支付的现金",
                    },
                }
                if (label in aggregate_rows.get(item_id, set())
                        and state.labels.get(item_id) in aggregate_rows.get(item_id, set())):
                    state.amounts[item_id] += amount
                    state.labels[item_id] = f"{state.labels[item_id]}、{label}"
                    continue
                state.issues.append(f"Conflicting amounts for item {item_id}: {state.amounts[item_id]} and {amount}")
            continue
        state.amounts[item_id], state.labels[item_id] = amount, label
        if fuzzy_match:
            state.fuzzy_rows[item_id] = {"line": number, "label": label,
                                         "amount": amount, "score": fuzzy_score}
        if item_id in range(OTHER_OPERATING_INCOME_ID, ASSET_DISPOSAL_INCOME_ID + 1):
            state.signs[item_id] = (-1 if item_id == CREDIT_IMPAIRMENT_ID
                                    and raw.startswith(("减：", "减:")) else
                                    1 if item_id == CREDIT_IMPAIRMENT_ID else
                                    1 if raw.startswith(("加：", "加:")) else
                                    -1 if raw.startswith(("减：", "减:")) else
                                    (1 if amount is not None and amount < 0 else -1)
                                    if "损失" in label and "损失以" not in raw else 1)
        if amount is not None and amount_column == 2 and column == 1:
            state.shifted_items.add(item_id)
        if (amount is not None and amount_column == column == 2 and len(cells) >= 4
                and not clean_cell(cells[1]) and not clean_cell(cells[-1])):
            state.ambiguous_rows[item_id] = number
    return state


def evaluate_equation(equation, state, absolute_tolerance, relative_tolerance):
    values = state.amounts
    amount = values.get(equation.target) or Decimal(0)
    total = sum(((values.get(item_id) or Decimal(0)) * sign for item_id, sign in equation.terms), Decimal(0))
    missing = [item_id for item_id in equation.required_terms if item_id not in values]
    tolerance = max(absolute_tolerance, relative_tolerance * max(abs(amount), abs(total)))
    return {"check": equation.name, "item_id": equation.target,
            "children": [item_id for item_id, _ in equation.terms],
            "coefficients": {str(item_id): sign for item_id, sign in equation.terms},
            "amount": str(amount), "children_sum": str(total),
            "difference": str(abs(amount - total)), "tolerance": str(tolerance),
            "missing_item_ids": missing, "passed": not missing and abs(amount - total) <= tolerance}


def resolve_shifted_amounts(state, equations, required_ids, absolute_tolerance, relative_tolerance):
    """Only drop an evidenced prior-period value when one omission reconciles safely."""
    adjustments = []
    for _ in range(len(state.ambiguous_rows)):
        before = [evaluate_equation(eq, state, absolute_tolerance, relative_tolerance) for eq in equations]
        chosen = None
        for equation, check in zip(equations, before):
            participants = {equation.target, *(item_id for item_id, _ in equation.terms)}
            if check["passed"] or check["missing_item_ids"] or not participants & state.shifted_items:
                continue
            candidates = []
            for item_id, _ in equation.terms:
                value = state.amounts.get(item_id)
                if item_id not in state.ambiguous_rows or item_id in required_ids or not value:
                    continue
                state.amounts[item_id] = None
                after = [evaluate_equation(eq, state, absolute_tolerance, relative_tolerance) for eq in equations]
                state.amounts[item_id] = value
                index = equations.index(equation)
                if after[index]["passed"] and all(not old["passed"] or new["passed"] for old, new in zip(before, after)):
                    candidates.append((item_id, value, after[index]["children_sum"]))
            if len(candidates) == 1:
                chosen = (equation, check, candidates[0])
                break
        if chosen is None:
            break
        equation, check, (item_id, value, total) = chosen
        state.amounts[item_id] = None
        adjustments.append({"item_id": item_id, "line": state.ambiguous_rows[item_id],
                            "parent_id": equation.target, "excluded_amount": str(value),
                            "sum_with": check["children_sum"], "sum_without": total,
                            "reason": "Only this ambiguous shifted-row omission reconciles without breaking passing equations"})
    return adjustments


def validate_fuzzy_matches(state, validation_context, equation_builder,
                           absolute_tolerance, relative_tolerance):
    """Keep fuzzy mappings only when their amount reconciles in a subtotal equation."""
    while True:
        validation_result = {**validation_context, "checks": [], "issues": []}
        equations = equation_builder(state, validation_result)
        rejected = []
        accepted = []
        for item_id, row in state.fuzzy_rows.items():
            participating = [equation for equation in equations
                             if equation.target == item_id
                             or any(child_id == item_id for child_id, _ in equation.terms)]
            checks = [evaluate_equation(equation, state, absolute_tolerance, relative_tolerance)
                      for equation in participating]
            if row["amount"] is None or not checks or any(not check["passed"] for check in checks):
                rejected.append((item_id, row))
                continue
            accepted.append({"item_id": item_id, "line": row["line"], "label": row["label"],
                             "similarity": round(row["score"], 3),
                             "validated_by": [equation.name for equation in participating]})
        if not rejected:
            return accepted
        for item_id, row in rejected:
            state.amounts.pop(item_id, None)
            state.labels.pop(item_id, None)
            state.signs.pop(item_id, None)
            state.unmapped_rows.append({"line": row["line"], "label": row["label"]})
            state.fuzzy_rows.pop(item_id, None)


def check_statement(row, chart, statement, required_ids, equation_builder, *,
                    absolute_tolerance=Decimal("0.01"), relative_tolerance=Decimal("0.0001"),
                    required_numeric_ids=()):
    result = {"pdf_hash": row["pdf_hash"], "statement": statement, "passed": False, "issues": [],
              "checks": [], "unmapped_rows": [], "items": [],
              "tolerance": {"absolute_statement_units": str(absolute_tolerance), "relative": str(relative_tolerance)},
              "_stock_code": row.get("stock_code")}
    markdown, multiplier = check_report_metadata({**row, "balance_sheet": row.get(statement)}, result)
    result["issues"] = [issue.replace("balance_sheet", statement) for issue in result["issues"]]
    if row.get("stock_code") and not re.fullmatch(r"\d{6}", str(row["stock_code"])):
        result["issues"].append("Invalid stock_code")
    state = parse_statement(markdown, chart, statement)
    result["issues"].extend(state.issues)
    fuzzy_matches = validate_fuzzy_matches(
        state, result, equation_builder, absolute_tolerance, relative_tolerance)
    equations = equation_builder(state, result)
    if fuzzy_matches:
        result["fuzzy_matches"] = fuzzy_matches
    result["unmapped_rows"] = state.unmapped_rows
    if state.unmapped_rows:
        result["issues"].append(f"Unmapped {statement} rows: {len(state.unmapped_rows)}")
    if not any(amount is not None for amount in state.amounts.values()):
        result["issues"].append("No numeric statement amounts")
    missing = [item_id for item_id in required_ids if item_id not in state.amounts
               or item_id in required_numeric_ids and state.amounts[item_id] is None]
    result["checks"].append({"check": "required_items", "checked": len(required_ids),
                             "passed": not missing, "failed_item_ids": missing})
    if missing:
        result["issues"].append("Required items missing or blank: " + ", ".join(map(str, missing)))
    adjustments = resolve_shifted_amounts(state, equations, required_ids, absolute_tolerance, relative_tolerance)
    if adjustments:
        result["column_adjustments"] = adjustments
    for equation in equations:
        check = evaluate_equation(equation, state, absolute_tolerance, relative_tolerance)
        result["checks"].append(check)
        if not check["passed"]:
            expression = " + ".join(f"{sign} × {state.amounts.get(item_id) or 0}({chart._items_by_id[item_id].name}, {item_id})"
                                    for item_id, sign in equation.terms)
            print_equation(
                row["pdf_hash"], equation.target,
                chart._items_by_id[equation.target].name,
                check["amount"], expression, check["children_sum"],
                check["difference"], check["tolerance"], check["passed"],
            )
        if not check["passed"]:
            result["issues"].append(f"Item {equation.target} ({equation.name}): {check['amount']} differs from {check['children_sum']}"
                                     + (f"; missing {check['missing_item_ids']}" if check["missing_item_ids"] else ""))
    per_share_ids = {EPS_ID, EPS_DILUTED_ID} if statement == "income_statement" else set()
    for item_id, amount in state.amounts.items():
        if amount is not None and item_id not in {NET_PROFIT_OPERATIONS_ID,
                                                   NET_PROFIT_OWNERSHIP_ID,
                                                   PER_SHARE_ITEMS_ID}:
            result["items"].append({"item_id": item_id, "english_name": chart._items_by_id[item_id].english_name,
                                    "statement_amount": str(amount), "amount_unit": "元/股" if item_id in per_share_ids else "元",
                                    "amount": str(amount if item_id in per_share_ids else amount * multiplier) if multiplier else None})
    result["passed"] = not result["issues"]
    result.pop("_stock_code", None)
    return result


def run_cli(statement, check_report, argv=None):
    def load_rows(storage, args):
        options = {"pdf_hash": args.pdf_hash, "retry": args.retry}
        if args.ex:
            options["ex"] = True
        if args.stock_code:
            options["stock_code"] = args.stock_code
        return storage.get_statement_reports(statement, **options)

    def save_row(storage, row, result, args, retry_failure=False):
        options = {"allow_any_stage": True} if args.pdf_hash or (args.retry and not retry_failure) else {}
        if args.ex:
            options["ex"] = True
        storage.store_statement_digestion(statement, row, result, **options)

    return run_digest_cli(
        statement, check_report, load_rows, save_row, argv,
        description=f"Validate and digest {statement} reports",
        absolute_tolerance=Decimal("0.01"), relative_tolerance=Decimal("0.0001"),
        retry_help=f"Retry reports at the {statement.replace('_', '-')} error stage",
        storage_class=LineItemStorage,
    )
