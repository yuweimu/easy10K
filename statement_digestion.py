"""Shared parsing, signed accounting equations, and CLI for statement digestion."""

from dataclasses import dataclass, field
from decimal import Decimal
import re

from digest_bs import check_report_metadata, clean_cell, parse_amount, select_amount_column
from digest_cli import print_equation, run_digest_cli
from line_item_chart import LineItem, LineItemChart
from line_item_storage import LineItemStorage
from statement_parsing import split_cells


@dataclass
class ParsedStatement:
    amounts: dict[int, Decimal | None] = field(default_factory=dict)
    labels: dict[int, str] = field(default_factory=dict)
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
    label = re.sub(r"^(?:其中|加|减)[：:]", "", label)
    label = re.sub(r"（减少）", "", label)
    label = re.sub(r"（[^）]*(?:净亏损|亏损总额|亏损|损失)[^）]*）$", "", label)
    label = re.sub(r"（(?:人民币)?元(?:/股)?）", "", label)
    return label.rstrip("：:")


def parse_statement(markdown, chart, statement):
    state = ParsedStatement()
    allowed = lambda item_id: 41 <= int(str(item_id)[:2]) <= 59 if statement == "income_statement" else 81 <= int(str(item_id)[:2]) <= 87
    names = {name: item for name, item in chart.items.items() if allowed(item.item_id)}

    def find_item(label):
        item = next((item for name, item in names.items() if normalize_label(name) == normalize_label(label)), None)
        if item is None and len(label) >= 8:
            item = next((item for name, item in names.items()
                         if normalize_label(name).startswith(normalize_label(label))), None)
        return item

    aliases = {"归属于母公司所有者的净利润": 560201, "归属于母公司所有者的其他综合收益的税后净额": 5701,
               "归属于母公司股东的其他综合收益的税后净额": 5701,
               "归属于母公司股东的综合收益总额": 5801,
               "投资损失": 44, "公允价值变动损失": 47, "资产处置损失": 50}
    amount_column, finance_details, oci_group = 1, False, None
    in_operating_costs = False
    previous_label = previous_full_name = ""
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
        item = find_item(label)
        item_id = item.item_id if item else aliases.get(label) if statement == "income_statement" else None
        if statement == "income_statement":
            if label == "资产减值损失" and in_operating_costs:
                item_id = 4227
            if label == "利息收入" and finance_details:
                item_id = 422503
            elif label == "其他" and oci_group in (570101, 570103):
                column = statement_amount_column(cells)
                amount_text = clean_cell(cells[column]) if len(cells) > column else ""
                try:
                    amount = parse_amount(amount_text) if amount_text else None
                except ValueError:
                    amount = None
                if amount is not None and amount != 0:
                    item_id = dynamic_other_item(oci_group)
            if item_id in (4225, 422501, 422503):
                finance_details = True
            elif item_id is not None:
                finance_details = False
            if item_id in (570101, 570103):
                oci_group = item_id
            in_operating_costs = item_id is not None and (
                item_id == 42 or str(item_id).startswith("42")
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
            state.issues.append(f"Line {number}: {label}: {error}")
            continue
        if item_id in state.amounts and state.amounts[item_id] is not None:
            if amount is not None and amount != state.amounts[item_id]:
                state.issues.append(f"Conflicting amounts for item {item_id}: {state.amounts[item_id]} and {amount}")
            continue
        state.amounts[item_id], state.labels[item_id] = amount, label
        if item_id in (43, 44, 45, 46, 47, 48, 49, 50):
            state.signs[item_id] = (1 if raw.startswith(("加：", "加:")) else
                                    -1 if raw.startswith(("减：", "减:")) or "损失" in label
                                    and "损失以" not in raw else 1)
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


def check_statement(row, chart, statement, required_ids, equation_builder, *,
                    absolute_tolerance=Decimal("0.01"), relative_tolerance=Decimal("0.0001"),
                    required_numeric_ids=(), emit_equations=True):
    result = {"pdf_hash": row["pdf_hash"], "statement": statement, "passed": False, "issues": [],
              "checks": [], "unmapped_rows": [], "items": [],
              "tolerance": {"absolute_statement_units": str(absolute_tolerance), "relative": str(relative_tolerance)}}
    markdown, multiplier = check_report_metadata({**row, "balance_sheet": row.get(statement)}, result)
    result["issues"] = [issue.replace("balance_sheet", statement) for issue in result["issues"]]
    if row.get("stock_code") and not re.fullmatch(r"\d{6}", str(row["stock_code"])):
        result["issues"].append("Invalid stock_code")
    state = parse_statement(markdown, chart, statement)
    result["issues"].extend(state.issues)
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
    equations = equation_builder(state, result)
    adjustments = resolve_shifted_amounts(state, equations, required_ids, absolute_tolerance, relative_tolerance)
    if adjustments:
        result["column_adjustments"] = adjustments
    for equation in equations:
        check = evaluate_equation(equation, state, absolute_tolerance, relative_tolerance)
        result["checks"].append(check)
        if emit_equations or not check["passed"]:
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
    per_share_ids = {5901, 5903} if statement == "income_statement" else set()
    for item_id, amount in state.amounts.items():
        if amount is not None and item_id not in {5601, 5602, 59}:
            result["items"].append({"item_id": item_id, "english_name": chart._items_by_id[item_id].english_name,
                                    "statement_amount": str(amount), "amount_unit": "元/股" if item_id in per_share_ids else "元",
                                    "amount": str(amount if item_id in per_share_ids else amount * multiplier) if multiplier else None})
    result["passed"] = not result["issues"]
    return result


def run_cli(statement, check_report, argv=None):
    def load_rows(storage, args):
        options = {"pdf_hash": args.pdf_hash, "retry": args.retry}
        if args.stock_code:
            options["stock_code"] = args.stock_code
        return storage.get_statement_reports(statement, **options)

    def save_row(storage, row, result, args, retry_failure=False):
        options = {"allow_any_stage": True} if args.pdf_hash or (args.retry and not retry_failure) else {}
        storage.store_statement_digestion(statement, row, result, **options)

    return run_digest_cli(
        statement, check_report, load_rows, save_row, argv,
        description=f"Validate and digest {statement} reports",
        absolute_tolerance=Decimal("0.01"), relative_tolerance=Decimal("0.0001"),
        retry_help=f"Retry reports at the {statement.replace('_', '-')} error stage",
        storage_class=LineItemStorage,
    )
