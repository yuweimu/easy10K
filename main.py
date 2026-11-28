# main.py
import logging
import json
import re
from html import escape
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse, Response
from pydantic import BaseModel
from item_ids import (
    EPS_DILUTED_ID,
    EPS_ID,
    OPERATING_COST_ID,
    OPERATING_COST_DETAIL_ID,
    OPERATING_REVENUE_DETAIL_ID,
    OPERATING_REVENUE_ID,
)
from line_item_chart import LineItemChart
from line_item_storage import LineItemStorage, ReportDigestionStage, StockEnrollment
from report_renderer import render_report
from report_sources import locate_source, render_page

# 1. Initialize the FastAPI application
app = FastAPI()
logger = logging.getLogger(__name__)
line_item_chart = LineItemChart()
REPORTS_DIR = Path(__file__).resolve().parent / "preprocessed"
PROJECT_DIR = Path(__file__).resolve().parent
STATEMENT_STYLES = (PROJECT_DIR / "report_view" / "statement.css").read_text(encoding="utf-8")
SOURCE_CACHE_DIR = PROJECT_DIR / "reports" / ".source-cache"
PUBLIC_STATEMENTS = {
    "balance-sheet": ("balance_sheet", "Balance Sheet", "资产负债表"),
    "income-statement": ("income_statement", "Income Statement", "利润表"),
    "cash-flow": ("cash_flow_statement", "Cash Flow Statement", "现金流量表"),
    "cash-flow-statement": ("cash_flow_statement", "Cash Flow Statement", "现金流量表"),
}
PUBLIC_AMOUNT_UNITS = {"元": 1, "千元": 1_000, "万元": 10_000, "百万元": 1_000_000, "亿元": 100_000_000}
HIDDEN_BALANCE_SHEET_ITEM_IDS = {110110, 210108}
CALCULATED_GROSS_PROFIT_ID = 600000
OTHER_PROFIT_LOSS_GROUP_ID = 600001
OTHER_PROFIT_LOSS_SOURCE_IDS = set(range(43, 51)) | {4227}

# 2. Define the structure of the data we expect (Pydantic Model)
class WordInput(BaseModel):
    """Defines the structure for the data we expect from the user."""
    word_input: str

# --- API Endpoints ---


class ReportStageUpdate(BaseModel):
    digestion_stage: ReportDigestionStage


class StockEnrollmentUpdate(BaseModel):
    enrollment: StockEnrollment


@app.patch("/stock-metadata/{stock_code}/enrollment")
def update_stock_enrollment(stock_code: str, data: StockEnrollmentUpdate) -> dict:
    """Manually set a stock's enrollment status from the stock reports UI."""
    with LineItemStorage() as storage:
        if not storage.database_url:
            raise HTTPException(status_code=503, detail="DATABASE_URL is not configured")
        try:
            return storage.update_stock_enrollment(stock_code, data.enrollment)
        except Exception as exc:
            logger.warning("Unable to update stock enrollment", exc_info=True)
            raise HTTPException(status_code=503, detail="Unable to update stock status") from exc


@app.patch("/report-digestion/{pdf_hash}/stage")
def update_report_digestion_stage(pdf_hash: str, data: ReportStageUpdate) -> dict:
    """Manually set the stage of an existing report selected by PDF hash."""
    with LineItemStorage() as storage:
        if not storage.database_url:
            raise HTTPException(status_code=503, detail="DATABASE_URL is not configured")
        try:
            result = storage.update_report_digestion_stage(pdf_hash, data.digestion_stage)
        except Exception as exc:
            logger.warning("Unable to update report digestion stage", exc_info=True)
            raise HTTPException(status_code=503, detail="Unable to update report stage") from exc
        if result is None:
            raise HTTPException(status_code=404, detail="Report not found")
        return result


@app.get("/report-digestion")
def get_report_digestion(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=25, ge=1, le=100),
    show_passed: bool = False,
    only_manual_pass: bool = False,
    report_group: Literal["InProgress", "FileErr", "ExtractOK", "ExtractErr", "AuditOK", "AuditErr",
                          "BalanceErr", "IncomeErr", "CashFlowErr", "CashFlowOK",
                          "ManualPass", "Deprecated", "Search"] | None = None,
    pdf_hash: str | None = Query(default=None, max_length=128),
    stock_code: str | None = Query(default=None, max_length=12),
    reporting_date: str | None = Query(default=None, max_length=10),
    digestion_stage: ReportDigestionStage | None = None,
) -> dict:
    """Return a paginated, read-only view of report digestion records."""
    with LineItemStorage() as storage:
        if not storage.database_url:
            raise HTTPException(status_code=503, detail="DATABASE_URL is not configured")
        try:
            return storage.get_report_digestion(
                page, page_size, show_passed=show_passed, only_manual_pass=only_manual_pass,
                report_group=report_group, pdf_hash=pdf_hash, stock_code=stock_code,
                reporting_date=reporting_date, digestion_stage=digestion_stage)
        except Exception as exc:
            logger.warning("Unable to fetch report digestion", exc_info=True)
            raise HTTPException(status_code=503, detail="Unable to fetch report digestion") from exc


@app.get("/report-digestion/file")
def get_digestion_pdf(pdf_hash: str = Query(min_length=1)) -> FileResponse:
    """Serve a registered local PDF inline, restricted to the reports directory."""
    with LineItemStorage() as storage:
        if not storage.database_url:
            raise HTTPException(status_code=503, detail="DATABASE_URL is not configured")
        try:
            local_path = storage.get_digestion_pdf_path(pdf_hash)
        except Exception as exc:
            logger.warning("Unable to fetch digestion PDF path", exc_info=True)
            raise HTTPException(status_code=503, detail="Unable to fetch registered PDF") from exc
    if not local_path:
        raise HTTPException(status_code=404, detail="Registered PDF not found")
    path = (PROJECT_DIR / local_path).resolve()
    if not path.is_relative_to((PROJECT_DIR / "reports").resolve()):
        raise HTTPException(status_code=403, detail="File is outside report directories")
    if path.suffix.lower() != ".pdf" or not path.is_file():
        raise HTTPException(status_code=404, detail="Report PDF not found")
    return FileResponse(path, media_type="application/pdf", filename=path.name,
                        content_disposition_type="inline", headers={"X-Content-Type-Options": "nosniff"})


@app.get("/report-registry")
def get_report_registry(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=25, ge=1, le=100),
) -> dict:
    """Return a paginated, read-only view of registered reports."""
    with LineItemStorage() as storage:
        if not storage.database_url:
            raise HTTPException(status_code=503, detail="DATABASE_URL is not configured")
        try:
            return storage.get_report_registry(page, page_size)
        except Exception as exc:
            logger.warning("Unable to fetch report registry", exc_info=True)
            raise HTTPException(status_code=503, detail="Unable to fetch report registry") from exc


@app.get("/report-registry/file/{file_kind}")
def get_registry_file(
    file_kind: Literal["pdf_local_path", "markdown_file", "preprocessed_file"],
    pdf_hash: str = Query(min_length=1),
    pdf_creation_time: str = Query(min_length=1),
) -> Response:
    """Open a registered PDF inline or render its Markdown in the report viewer."""
    with LineItemStorage() as storage:
        if not storage.database_url:
            raise HTTPException(status_code=503, detail="DATABASE_URL is not configured")
        try:
            files = storage.get_report_files(pdf_hash, pdf_creation_time)
        except Exception as exc:
            logger.warning("Unable to fetch registered file", exc_info=True)
            raise HTTPException(status_code=503, detail="Unable to fetch registered file") from exc
    if not files or not files.get(file_kind):
        raise HTTPException(status_code=404, detail="Registered file not found")
    # Resolve only database-selected paths, never an arbitrary path from the URL.
    path = (PROJECT_DIR / files[file_kind]).resolve()
    allowed_roots = [(PROJECT_DIR / folder).resolve() for folder in ("reports", "markdown", "preprocessed")]
    if not any(path.is_relative_to(root) for root in allowed_roots):
        raise HTTPException(status_code=403, detail="File is outside report directories")
    expected_suffix = ".pdf" if file_kind == "pdf_local_path" else ".md"
    if path.suffix.lower() != expected_suffix or not path.is_file():
        raise HTTPException(status_code=404, detail="Report file not found")
    if file_kind == "pdf_local_path":
        return FileResponse(path, media_type="application/pdf", filename=path.name,
                            content_disposition_type="inline", headers={"X-Content-Type-Options": "nosniff"})
    return render_report(path.read_text(encoding="utf-8"), title=path.name)


@app.get("/reports/{stock_code}/{reporting_date}", response_class=HTMLResponse)
def get_report(stock_code: str, reporting_date: str) -> HTMLResponse:
    """Render the Markdown report as a browser-readable HTML page."""
    report_path = (REPORTS_DIR / stock_code / f"{reporting_date}.md").resolve()
    reports_dir = REPORTS_DIR.resolve()
    if not report_path.is_relative_to(reports_dir):
        raise HTTPException(status_code=400, detail="Invalid report path")

    if not report_path.is_file():
        raise HTTPException(status_code=404, detail="Report not found")

    return render_report(
        report_path.read_text(encoding="utf-8"),
        title=f"{stock_code} · {reporting_date}",
    )


@app.get("/stocks/{stock_code}/{statement_slug}", response_class=HTMLResponse)
def get_public_statement_page(
    stock_code: str,
    statement_slug: str,
    language: Literal["zh", "en"] = Query(default="zh", alias="lang"),
) -> HTMLResponse:
    """Render an indexable HTML statement page for one stock and statement."""
    # When called directly in tests, FastAPI's Query default is not resolved.
    if language not in ("zh", "en"):
        language = "zh"
    if not re.fullmatch(r"\d{6}", stock_code):
        raise HTTPException(status_code=404, detail="Stock not found")
    statement_info = PUBLIC_STATEMENTS.get(statement_slug)
    if statement_info is None:
        raise HTTPException(status_code=404, detail="Statement not found")

    statement, title_en, title_zh = statement_info
    item_name_column_width = 272 if statement == "cash_flow_statement" else 250
    with LineItemStorage() as storage:
        if not storage.database_url:
            raise HTTPException(status_code=503, detail="DATABASE_URL is not configured")
        try:
            items = storage.get_statement_items(statement, stock_code)
            available_stocks = storage.get_statement_stock_codes(statement)
            stock = next(
                (entry for entry in available_stocks
                 if entry["stock_code"] == stock_code),
                None,
            )
        except Exception as exc:
            logger.warning("Unable to render public statement page", exc_info=True)
            raise HTTPException(status_code=503, detail="Unable to render statement") from exc

    if not items:
        raise HTTPException(status_code=404, detail="Statement not found")

    stock_name = (stock or {}).get("stock_name") or stock_code
    dates = sorted({str(item["reporting_date"]) for item in items}, reverse=True)
    latest_items: dict[tuple[str, int], dict] = {}
    for item in items:
        latest_items[(str(item["reporting_date"]), int(item["item_id"]))] = item
    if statement == "income_statement":
        # Older extracted reports may use 4227 for the standalone asset
        # impairment line now represented by item 49. Normalize only when the
        # same reporting period does not already contain item 49.
        for date in dates:
            legacy_item = latest_items.pop((date, 4227), None)
            if legacy_item is not None and (date, 49) not in latest_items:
                legacy_item = {**legacy_item, "item_id": 49}
                latest_items[(date, 49)] = legacy_item
            elif legacy_item is not None:
                latest_items[(date, 4227)] = legacy_item
        # Keep the classification headings and earnings-per-share group
        # visible even when extraction omitted them; these rows are display-only.
        for date in dates:
            for item_id in (5601, 5602, 59):
                latest_items[(date, item_id)] = {
                    "amount": None,
                    "item_id": item_id,
                    "reporting_date": date,
                }
            latest_items.setdefault((date, 560103), {
                "amount": None,
                "item_id": 560103,
                "reporting_date": date,
            })
    calculated_item_labels: dict[int, tuple[str, str]] = {}
    if statement == "income_statement":
        calculated_item_labels[CALCULATED_GROSS_PROFIT_ID] = ("毛利润", "Gross profit")
        for date in dates:
            revenue_item = latest_items.get((date, OPERATING_REVENUE_DETAIL_ID))
            cost_item = latest_items.get((date, OPERATING_COST_DETAIL_ID))
            if not revenue_item or not cost_item:
                continue
            try:
                revenue = Decimal(str(revenue_item.get("amount")))
                cost = Decimal(str(cost_item.get("amount")))
            except (InvalidOperation, TypeError, ValueError):
                continue
            latest_items[(date, CALCULATED_GROSS_PROFIT_ID)] = {
                "amount": revenue - cost,
                "item_id": CALCULATED_GROSS_PROFIT_ID,
                "reporting_date": date,
            }
    hidden_item_ids = HIDDEN_BALANCE_SHEET_ITEM_IDS if statement == "balance_sheet" else set()
    item_ids = sorted({item_id for _, item_id in latest_items if item_id not in hidden_item_ids})
    available_item_ids = set(item_ids)
    children_by_parent: dict[int, list[int]] = {}
    root_item_ids: list[int] = []
    for item_id in item_ids:
        parent_id = item_id // 100
        if parent_id > 0 and parent_id in available_item_ids:
            children_by_parent.setdefault(parent_id, []).append(item_id)
        else:
            root_item_ids.append(item_id)
    root_item_ids = [item_id for item_id in root_item_ids if item_id != CALCULATED_GROSS_PROFIT_ID]
    ordered_item_ids: list[int] = []
    indent_levels: dict[int, int] = {}
    has_calculated_gross_profit = any(
        (date, CALCULATED_GROSS_PROFIT_ID) in latest_items for date in dates
    )

    def append_item(item_id: int, indent_level: int) -> None:
        ordered_item_ids.append(item_id)
        indent_levels[item_id] = indent_level
        for child_id in sorted(children_by_parent.get(item_id, [])):
            append_item(child_id, indent_level + 1)
            if (
                statement == "income_statement"
                and item_id == OPERATING_COST_ID
                and child_id == OPERATING_COST_DETAIL_ID
                and has_calculated_gross_profit
            ):
                ordered_item_ids.append(CALCULATED_GROSS_PROFIT_ID)
                indent_levels[CALCULATED_GROSS_PROFIT_ID] = indent_level + 1

    for root_id in root_item_ids:
        append_item(root_id, 0)
    other_profit_loss_group_enabled = False
    other_profit_loss_detail_ids: set[int] = set()
    if statement == "income_statement":
        source_roots = [
            item_id for item_id in ordered_item_ids
            if item_id in OTHER_PROFIT_LOSS_SOURCE_IDS
            and item_id // 100 not in OTHER_PROFIT_LOSS_SOURCE_IDS
        ]
        for source_id in source_roots:
            source_index = ordered_item_ids.index(source_id)
            source_indent = indent_levels[source_id]
            for candidate_id in ordered_item_ids[source_index:]:
                if candidate_id != source_id and indent_levels[candidate_id] <= source_indent:
                    break
                other_profit_loss_detail_ids.add(candidate_id)
        if other_profit_loss_detail_ids:
            for date in dates:
                values = []
                for source_id in source_roots:
                    value = latest_items.get((date, source_id), {}).get("amount")
                    if value is None or str(value).strip() == "":
                        continue
                    try:
                        values.append(Decimal(str(value)))
                    except (InvalidOperation, TypeError, ValueError):
                        continue
                if values:
                    latest_items[(date, OTHER_PROFIT_LOSS_GROUP_ID)] = {
                        "amount": sum(values, Decimal(0)),
                        "item_id": OTHER_PROFIT_LOSS_GROUP_ID,
                        "reporting_date": date,
                    }
            group_position = min(
                index for index, item_id in enumerate(ordered_item_ids)
                if item_id in other_profit_loss_detail_ids
            )
            details_in_order = [
                item_id for item_id in ordered_item_ids
                if item_id in other_profit_loss_detail_ids
            ]
            filtered_item_ids = [
                item_id for item_id in ordered_item_ids
                if item_id not in other_profit_loss_detail_ids
            ]
            filtered_position = sum(
                item_id not in other_profit_loss_detail_ids
                for item_id in ordered_item_ids[:group_position]
            )
            ordered_item_ids = (
                filtered_item_ids[:filtered_position]
                + [OTHER_PROFIT_LOSS_GROUP_ID]
                + details_in_order
                + filtered_item_ids[filtered_position:]
            )
            indent_levels[OTHER_PROFIT_LOSS_GROUP_ID] = 0
            for item_id in details_in_order:
                indent_levels[item_id] += 1
            calculated_item_labels[OTHER_PROFIT_LOSS_GROUP_ID] = (
                "\u00a0加：其他非主营业务损益", "Other profit and loss items"
            )
            other_profit_loss_group_enabled = True
    chart_items = {item.item_id: item for item in line_item_chart._items_by_id.values()}

    def amount_text(value: object, *, per_share: bool) -> str:
        if value is None or str(value).strip() == "":
            return "—"
        try:
            number = Decimal(str(value))
            if not per_share:
                number /= Decimal(PUBLIC_AMOUNT_UNITS["百万元"])
            if number == 0:
                return "—"
            formatted = f"{number:,.4f}" if per_share else f"{number:,.2f}"
            if per_share:
                integer, _, fraction = formatted.partition(".")
                fraction = fraction.rstrip("0")
                fraction += "0" * max(0, 2 - len(fraction))
                formatted = f"{integer}.{fraction}"
            return escape(formatted)
        except (InvalidOperation, ValueError):
            return escape(str(value))

    cash_flow_label_overrides = {
        81: "一、经营活动产生的现金流量",
        82: "二、投资活动产生的现金流量",
        83: "三、筹资活动产生的现金流量",
        84: "四、汇率变动对现金及现金等价物的影响",
        85: "五、现金及现金等价物净增加额",
        86: "\u00a0\u00a0加：期初现金及现金等价物余额",
        87: "六、期末现金及现金等价物余额",
    }
    income_statement_label_overrides = {
        41: "一、营业总收入",
        42: "二、营业总成本",
        51: "三、营业利润",
        52: "\u00a0加：营业外收入",
        53: "\u00a0减：营业外支出",
        54: "四、利润总额",
        55: "\u00a0减：所得税费用",
        56: "五、净利润",
        5601: "(一)按经营持续性分类",
        5602: "(二)按所有权归属分类",
        57: "六、其他综合收益的税后净额",
        58: "七、综合收益总额",
        59: "八、每股收益",
        5901: "(一)基本每股收益(元/股)",
	    5903: "(二)稀释每股收益(元/股)",
    }
    rows = []
    for item_id in ordered_item_ids:
        raw_values = [latest_items.get((date, item_id), {}).get("amount") for date in dates]
        numeric_values: list[Decimal] = []
        has_unparseable_value = False
        for value in raw_values:
            if value is None or str(value).strip() == "":
                continue
            try:
                numeric_values.append(Decimal(str(value)))
            except (InvalidOperation, ValueError):
                has_unparseable_value = True
                break
        if (item_id not in {OTHER_PROFIT_LOSS_GROUP_ID, 560103} and numeric_values
                and not has_unparseable_value and all(value == 0 for value in numeric_values)):
            continue
        if item_id in calculated_item_labels:
            label_cn, label_en = calculated_item_labels[item_id]
        else:
            chart_item = chart_items.get(item_id)
            label_cn = chart_item.name if chart_item else f"Item {item_id}"
            label_en = chart_item.english_name if chart_item else f"Item {item_id}"
        trend_label = label_cn if language == "zh" else label_en
        if statement == "cash_flow_statement":
            label_cn = cash_flow_label_overrides.get(item_id, label_cn)
        elif statement == "income_statement":
            label_cn = income_statement_label_overrides.get(item_id, label_cn)
        item_label = label_cn if language == "zh" else label_en
        item_name_markup = escape(item_label) + (
            "<sup>(1)</sup>" if item_id == CALCULATED_GROSS_PROFIT_ID
            else "<sup>(2)</sup>" if item_id == OTHER_PROFIT_LOSS_GROUP_ID
            else ""
        )
        item_language = "zh-CN" if language == "zh" else "en"
        per_share = statement == "income_statement" and item_id in {EPS_ID, EPS_DILUTED_ID}
        is_income_statement_blank_item = statement == "income_statement" and item_id in {5601, 5602, 59}
        is_income_classification_heading = statement == "income_statement" and item_id in {5601, 5602}
        gross_margin_values: dict[str, str] = {}
        if item_id in {CALCULATED_GROSS_PROFIT_ID, 51, 56}:
            for date in dates:
                gross_profit = latest_items.get((date, item_id), {}).get("amount")
                revenue_item = latest_items.get((date, OPERATING_REVENUE_DETAIL_ID))
                if revenue_item is None:
                    revenue_item = latest_items.get((date, OPERATING_REVENUE_ID))
                revenue = revenue_item.get("amount") if revenue_item else None
                try:
                    if revenue is not None and Decimal(str(revenue)) != 0:
                        gross_margin_values[date] = f"{Decimal(str(gross_profit)) / Decimal(str(revenue)) * 100:.4f}"
                except (InvalidOperation, TypeError, ValueError):
                    continue
        cells = "".join(
            f"<td class=\"amount-column\" data-reporting-date=\"{escape(date, quote=True)}\" data-amount=\"{escape(str(latest_items.get((date, item_id), {}).get('amount') if latest_items.get((date, item_id), {}).get('amount') is not None else ''), quote=True)}\" data-per-share=\"{'true' if per_share else 'false'}\" data-zero-display=\"{'blank' if is_income_statement_blank_item else ''}\" data-gross-margin=\"{gross_margin_values.get(date, '')}\">"
            f"{' ' if is_income_statement_blank_item else amount_text(latest_items.get((date, item_id), {}).get('amount'), per_share=per_share)}</td>"
            for date in dates
        )
        is_other_profit_loss_group = item_id == OTHER_PROFIT_LOSS_GROUP_ID
        is_other_profit_loss_detail = item_id in other_profit_loss_detail_ids
        initially_collapsed = (
            (statement == "cash_flow_statement" and item_id in {82, 83})
            or (statement == "income_statement" and item_id in {44, 56, 57, 58, 4225})
            or (statement == "balance_sheet" and item_id in {110128, 1102, 21, 31})
        )
        toggle = (
            f'<button type="button" class="item-toggle" data-other-profit-loss-toggle '
            f'aria-label="{escape("展开" if language == "zh" else "Show", quote=True)} {escape(item_label, quote=True)}" '
            f'aria-expanded="false"></button>'
            if is_other_profit_loss_group
            else (
            f'<button type="button" class="item-toggle" data-item-toggle="{item_id}" '
            f'aria-label="{escape(("展开" if language == "zh" else "Show") + " " + item_label)}" '
            f'aria-expanded="{"false" if initially_collapsed else "true"}"></button>'
            if item_id in children_by_parent
            else '<span class="item-toggle-placeholder" aria-hidden="true"></span>'
            )
        )
        trend_toggle = "" if is_income_statement_blank_item else (
            f'<button type="button" class="trend-toggle" data-trend-toggle="{item_id}" '
            f'title="{escape("查看趋势" if language == "zh" else "Show trend", quote=True)}" '
            f'aria-label="{escape(("查看" if language == "zh" else "Show") + " " + item_label + ("金额趋势" if language == "zh" else " amount trend"), quote=True)}" '
            f'aria-expanded="false" aria-controls="trend-{item_id}">'
            '<svg width="14" height="14" viewBox="0 0 16 16" fill="none" aria-hidden="true">'
            '<path d="M2 2v12h12" stroke="currentColor" stroke-width="1.2"/>'
            '<path d="M5 11V8m4 3V5m4 6V3" stroke="currentColor" stroke-width="2"/>'
            '</svg></button>'
        )
        is_first_level_parent = item_id in children_by_parent and indent_levels[item_id] == 0
        is_highlighted_statement_row = (
            not is_other_profit_loss_group
            and (
                is_first_level_parent
                or (statement == "income_statement" and item_id in {51, 54})
            )
        )
        group_class = " item-row-group" if is_highlighted_statement_row else ""
        classification_class = " income-classification-row" if is_income_classification_heading else ""
        detail_class = " other-profit-loss-detail" if is_other_profit_loss_detail else ""
        other_profit_loss_group_class = " other-profit-loss-group" if is_other_profit_loss_group else ""
        gross_profit_class = " gross-profit-row" if item_id == CALCULATED_GROSS_PROFIT_ID else ""
        hidden_attribute = " hidden" if is_other_profit_loss_detail else ""
        parent_attribute = (
            f' data-parent-id="{OPERATING_COST_ID}"'
            if item_id == CALCULATED_GROSS_PROFIT_ID else ""
        )
        row_html = (
            f'<tr class="item-row{group_class}{classification_class}{detail_class}{other_profit_loss_group_class}{gross_profit_class}" data-item-id="{item_id}"{parent_attribute}{hidden_attribute}><th scope="row" class="item-name" title="{escape(item_label, quote=True)}" style="padding-left: {10 + indent_levels[item_id] * 18:g}px"><div class="item-name-inner">{toggle}'
            f"<span lang=\"{item_language}\">{item_name_markup}</span>"
            f"{trend_toggle}</div></th>{cells}</tr>"
        )
        trend_row = (
            f'<tr class="trend-detail-row{detail_class}" id="trend-{item_id}" data-trend-item-id="{item_id}" hidden>'
            f'<td colspan="{len(dates) + 1}"><div class="trend-detail" data-trend-detail="{item_id}"><div class="trend-chart-scroll">'
            f'<div class="trend-chart-wrap"><svg class="trend-chart" data-trend-chart="{item_id}" data-trend-label="{escape(trend_label, quote=True)}" data-trend-stock="{escape(f"{stock_name} ({stock_code})", quote=True)}" viewBox="0 0 1075 320" role="img" aria-label="{escape(trend_label, quote=True)}"></svg></div>'
            '</div></div></td></tr>'
        )
        rows.append(row_html + trend_row)

    page_url = f"/stocks/{stock_code}/{statement_slug}"
    localized_title = title_zh if language == "zh" else title_en
    localized_description = (
        f"{title_zh}及{stock_name}（{stock_code}）各报告期数据。"
        if language == "zh" else
        f"{title_en} and reporting-period data for {stock_name} ({stock_code})."
    )
    title = f"{stock_name} ({stock_code}) {localized_title} | Easy10K"
    description = localized_description
    table_item_label = "项目" if language == "zh" else "Financial item"
    language_label = "语言" if language == "zh" else "Language"
    home_label = "首页" if language == "zh" else "Dashboard"
    amount_unit_label = "金额单位" if language == "zh" else "Amount unit"
    show_decimals_label = "显示小数" if language == "zh" else "Show decimals"
    stock_search_options = "".join(
        f'<button type="button" class="stock-suggestion" role="option" data-stock-code="{escape(str(entry.get("stock_code") or ""), quote=True)}" data-stock-name="{escape(str(entry.get("stock_name") or entry.get("stock_code") or ""), quote=True)}"><span>{escape(str(entry.get("stock_code") or ""))}</span><span>{escape(str(entry.get("stock_name") or entry.get("stock_code") or ""))}</span></button>'
        for entry in available_stocks
        if re.fullmatch(r"\d{6}", str(entry.get("stock_code") or ""))
    )
    stock_directory = {
        str(entry.get("stock_code")): str(entry.get("stock_name") or entry.get("stock_code"))
        for entry in available_stocks
        if re.fullmatch(r"\d{6}", str(entry.get("stock_code") or ""))
    }
    stock_directory_json = json.dumps(stock_directory, ensure_ascii=False).replace("<", "\\u003c")
    default_amount_unit = "亿元" if language == "zh" else "百万元"
    table_notes = []
    if statement == "income_statement" and any(
        (date, CALCULATED_GROSS_PROFIT_ID) in latest_items for date in dates
    ):
        table_notes.append(
            "(1) “毛利润”并非财务报表中正式列示的项目，本表格为便于阅读而引入，由营业收入减营业成本计算得出。"
            if language == "zh"
            else "(1) Gross profit is shown as a convenience calculation (operating revenue minus operating costs); it is not usually presented as a separate line item in the statement."
        )
    if statement == "income_statement" and other_profit_loss_group_enabled:
        table_notes.append(
            "(2) “其他非主营业务损益”并非财务报表中正式列示的项目，本表格引入该项目以合并展示非主营业务损益、投资及减值相关损益，从而突出经营性科目、改善表格阅读体验。"
            if language == "zh"
            else "(2) Other profit and loss items are not a formal statement line item; this group is included to improve the clarity and readability of the table as a whole."
        )
    table_notes_markup = "".join(f"<p>{escape(note)}</p>" for note in table_notes)

    def equation_number(value: object) -> str | None:
        """Format a reported amount using the unit shown by the page."""
        if value is None or str(value).strip() == "":
            return None
        try:
            number = Decimal(str(value)) / Decimal(PUBLIC_AMOUNT_UNITS[default_amount_unit])
            return f"{number:,.2f}"
        except (InvalidOperation, ValueError, KeyError):
            return None

    def item_label(item_id: int) -> tuple[str, str]:
        if item_id in calculated_item_labels:
            return calculated_item_labels[item_id]
        chart_item = chart_items.get(item_id)
        return (
            chart_item.name if chart_item else f"项目 {item_id}",
            chart_item.english_name if chart_item else f"Item {item_id}",
        )

    # Build examples from the newest report rather than showing only abstract
    # formulas.  Direct hierarchy children are the same relationships used by
    # the table digestion checks, so the displayed values are auditable in the
    # table above.  Keep the list short enough to remain useful as a disclaimer.
    latest_date = dates[0]
    actual_equations: list[tuple[str, str, str, str]] = []
    for parent_id in ordered_item_ids:
        child_ids = children_by_parent.get(parent_id, [])
        parent_value = equation_number(latest_items.get((latest_date, parent_id), {}).get("amount"))
        valued_children = [
            (child_id, equation_number(latest_items.get((latest_date, child_id), {}).get("amount")))
            for child_id in child_ids
        ]
        valued_children = [(child_id, value) for child_id, value in valued_children if value is not None]
        if parent_value is None or len(valued_children) < 2:
            continue
        parent_cn, parent_en = item_label(parent_id)
        terms_cn: list[str] = []
        terms_en: list[str] = []
        for index, (child_id, child_value) in enumerate(valued_children):
            child_cn, child_en = item_label(child_id)
            separator = " + "
            if statement == "cash_flow_statement" and index > 0 and "流出" in child_cn:
                separator = " - "
            terms_cn.append(f"{separator if index else ''}{child_cn} {child_value}")
            terms_en.append(f"{separator if index else ''}{child_en} {child_value}")
        result_with_unit = f"{parent_value}({default_amount_unit})"
        actual_equations.append((parent_cn, parent_en, f"{result_with_unit} =" + "".join(terms_cn), f"{result_with_unit} =" + "".join(terms_en)))

    if statement == "income_statement":
        net_profit_value = equation_number(latest_items.get((latest_date, 56), {}).get("amount"))
        net_profit_cn, net_profit_en = item_label(56)
        for child_ids in ((560101, 560103), (560201, 560203)):
            child_values = [
                equation_number(latest_items.get((latest_date, child_id), {}).get("amount"))
                for child_id in child_ids
            ]
            if net_profit_value is None or any(value is None for value in child_values):
                continue
            children_cn = [item_label(child_id)[0] for child_id in child_ids]
            children_en = [item_label(child_id)[1] for child_id in child_ids]
            result_with_unit = f"{net_profit_value}({default_amount_unit}) = "
            formula_cn = result_with_unit + " + ".join(
                f"{label} {value}" for label, value in zip(children_cn, child_values)
            )
            formula_en = result_with_unit + " + ".join(
                f"{label} {value}" for label, value in zip(children_en, child_values)
            )
            actual_equations.append((net_profit_cn, net_profit_en, formula_cn, formula_en))

    actual_equations.sort(
        key=lambda equation: (
            0 if any(marker in equation[2] for marker in ("持续经营净利润", "归属于母公司股东的净利润"))
            else 1 if any(marker in equation[0] for marker in ("合计", "总计", "净额", "余额", "净利润"))
            else 2
        )
    )
    actual_equations = actual_equations[:10]
    equation_disclaimer_title = "数字可靠性校验" if language == "zh" else "Reliability checks"
    equation_disclaimer_intro = (
        f"在财报消化阶段，我们对以下关系进行了额外校验，尽力让展示数字更可靠。以下示例取最新报告期 {latest_date}，单位：{default_amount_unit}；由于不同报告的列位、披露范围和会计列报可能不同，校验结果仅供辅助参考。"
        if language == "zh" else
        f"During digestion, we apply the following checks to make the displayed numbers more reliable. Examples use the latest reporting date, {latest_date}, in {default_amount_unit}. Report layouts, disclosure scope, and accounting presentation can vary, so these checks are supporting evidence rather than an accounting guarantee."
    )
    equation_lines = {
        "balance_sheet": (
            ("流动资产合计", "流动资产各明细项目之和"),
            ("非流动资产合计", "非流动资产各明细项目之和"),
            ("资产总计", "流动资产合计 + 非流动资产合计"),
            ("负债合计", "流动负债合计 + 非流动负债合计"),
            ("所有者权益合计", "归属于母公司所有者权益合计 + 少数股东权益"),
            ("负债和所有者权益总计", "负债合计 + 所有者权益合计"),
        ),
        "income_statement": (
            ("营业利润", "营业收入 - 营业成本 - 期间费用 + 其他经营调整项"),
            ("利润总额", "营业利润 + 营业外收入 - 营业外支出"),
            ("净利润", "利润总额 - 所得税费用"),
            ("综合收益总额", "净利润 + 其他综合收益"),
            ("归属母公司综合收益总额", "归属母公司净利润 + 归属母公司其他综合收益"),
        ),
        "cash_flow_statement": (
            ("经营活动现金流量净额", "经营活动现金流入小计 - 经营活动现金流出小计"),
            ("投资活动现金流量净额", "投资活动现金流入小计 - 投资活动现金流出小计"),
            ("筹资活动现金流量净额", "筹资活动现金流入小计 - 筹资活动现金流出小计"),
            ("现金及现金等价物净增加额", "经营、投资、筹资活动现金流量净额 + 汇率变动影响"),
            ("期末现金及现金等价物余额", "期初现金及现金等价物余额 + 净增加额"),
        ),
    }[statement]
    equation_labels_en = {
        "流动资产合计": "Total current assets",
        "非流动资产合计": "Total non-current assets",
        "资产总计": "Total assets",
        "负债合计": "Total liabilities",
        "所有者权益合计": "Total equity",
        "负债和所有者权益总计": "Total liabilities and equity",
        "营业利润": "Operating profit",
        "利润总额": "Profit before tax",
        "净利润": "Net profit",
        "综合收益总额": "Total comprehensive income",
        "归属母公司综合收益总额": "Comprehensive income attributable to owners",
        "经营活动现金流量净额": "Net cash flow from operating activities",
        "投资活动现金流量净额": "Net cash flow from investing activities",
        "筹资活动现金流量净额": "Net cash flow from financing activities",
        "现金及现金等价物净增加额": "Net increase in cash and cash equivalents",
        "期末现金及现金等价物余额": "Closing cash and cash equivalents",
    }
    equation_formulas_en = {
        "流动资产合计": "sum of current-asset detail items",
        "非流动资产合计": "sum of non-current-asset detail items",
        "资产总计": "total current assets + total non-current assets",
        "负债合计": "total current liabilities + total non-current liabilities",
        "所有者权益合计": "equity attributable to owners + non-controlling interests",
        "负债和所有者权益总计": "total liabilities + total equity",
        "营业利润": "revenue - operating costs - period expenses + other operating adjustments",
        "利润总额": "operating profit + non-operating income - non-operating expenses",
        "净利润": "profit before tax - income tax expense",
        "综合收益总额": "net profit + other comprehensive income",
        "归属母公司综合收益总额": "net profit attributable to owners + related other comprehensive income",
        "经营活动现金流量净额": "operating cash inflows - operating cash outflows",
        "投资活动现金流量净额": "investing cash inflows - investing cash outflows",
        "筹资活动现金流量净额": "financing cash inflows - financing cash outflows",
        "现金及现金等价物净增加额": "operating + investing + financing cash flows + exchange-rate effects",
        "期末现金及现金等价物余额": "opening cash and cash equivalents + net increase",
    }
    if actual_equations:
        equation_markup = "".join(
            f'<li><span>{escape(parent_cn if language == "zh" else parent_en)}</span><code>{escape(formula_cn if language == "zh" else formula_en)}</code></li>'
            for parent_cn, parent_en, formula_cn, formula_en in actual_equations
        )
    else:
        equation_markup = "".join(
            f'<li><span>{escape(label if language == "zh" else equation_labels_en[label])}</span><code>{escape(formula if language == "zh" else equation_formulas_en[label])}</code></li>'
            for label, formula in equation_lines
        )
    period_caption_labels = (
        {"03": "一季报", "06": "半年报", "09": "三季报", "12": "年报"}
        if language == "zh" else
        {"03": "Q1", "06": "Semiannual", "09": "Q3", "12": "Annual"}
    )

    def reporting_date_caption(date: str) -> str:
        match = re.fullmatch(r"(\d{4})-(03|06|09|12)-(30|31)", date)
        if not match:
            return date
        year, month, _day = match.groups()
        label = period_caption_labels[month]
        return f"{year}{label}" if language == "zh" else f"{label} {year}"

    amount_unit_options = "".join(
        f"<option value=\"{unit}\"{' selected' if unit == default_amount_unit else ''}>{unit if language == 'zh' else unit + ' CNY'}</option>"
        for unit in PUBLIC_AMOUNT_UNITS
    )
    statement_tabs = (
        ("balance-sheet", "资产负债表", "Balance Sheet"),
        ("income-statement", "利润表", "Income Statement"),
        ("cash-flow-statement", "现金流量表", "Cash Flow Statement"),
    )
    period_filters = (
        ("", "全部", "All"),
        ("12-31", "年报", "Annual"),
        ("06-30", "半年报", "Semiannual"),
        ("03-31", "一季报", "Q1"),
        ("09-30", "三季报", "Q3"),
    )
    statement_tabs_label = "财务报表" if language == "zh" else "Financial statements"
    period_filters_label = "报告期筛选" if language == "zh" else "Reporting period filter"
    statement_tab_links = "".join(
        f'<a class="{"active" if current_slug == statement_slug else ""}" href="/stocks/{escape(stock_code)}/{current_slug}?lang={language}" aria-current="{"page" if current_slug == statement_slug else "false"}">{escape(label_zh if language == "zh" else label_en)}</a>'
        for current_slug, label_zh, label_en in statement_tabs
    )
    period_filter_buttons = "".join(
        f'<button type="button" class="{"active" if ending == "" else ""}" data-period-ending="{ending}" aria-pressed="{"true" if ending == "" else "false"}">{escape(label_zh if language == "zh" else label_en)}</button>'
        for ending, label_zh, label_en in period_filters
    )
    structured_data = json.dumps({
        "@context": "https://schema.org",
        "@type": "Dataset",
        "name": title,
        "description": description,
        "url": page_url,
        "temporalCoverage": f"{dates[-1]}/{dates[0]}",
    }, ensure_ascii=False).replace("<", "\\u003c")
    html = f"""<!doctype html>
<html lang="{'zh-CN' if language == 'zh' else 'en'}">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{escape(title)}</title>
  <meta name="description" content="{escape(description)}">
  <link rel="canonical" href="{page_url}">
  <script type="application/ld+json">{structured_data}</script>
  <style>
    :root {{ --label-width: {item_name_column_width}px; }}
    {STATEMENT_STYLES}
  </style>
</head>
<body>
  <header class="masthead"><div class="masthead-inner">
      <div class="brand-line">
        <span class="brand-icon" aria-hidden="true">
          <svg viewBox="0 0 24 24" fill="none" aria-hidden="true">
            <path d="M4 19V5M4 19h16" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/>
            <path d="m7 15 3-4 3 2 5-7" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/>
          </svg>
        </span>
        <div class="site-logo" aria-label="ValuePaper 上市公司财报助手"><strong>ValuePaper</strong><span>{"财报数据助手" if language == "zh" else "Financial statements"}</span></div>
      </div>
      <div class="page-tools">
        <a class="home-link" href="/" aria-label="{escape(home_label)}">⌂ {escape(home_label)}</a>
        <nav class="language-switcher" aria-label="{escape(language_label)}">
            <div class="language-toggle">
            <a class="{'active' if language == 'en' else ''}" href="{page_url}?lang=en" lang="en" aria-current="{'page' if language == 'en' else 'false'}">EN</a>
            <a class="{'active' if language == 'zh' else ''}" href="{page_url}?lang=zh" lang="zh-CN" aria-current="{'page' if language == 'zh' else 'false'}">中文</a>
            </div>
        </nav>
      </div>

        <form class="stock-search" id="stock-search" role="search">
          <input id="stock-search-code" name="stock_code" type="search" autocomplete="off" inputmode="numeric" pattern="[0-9]{{6}}" maxlength="6" placeholder="{'股票代码' if language == 'zh' else 'Stock code'}" aria-label="{'搜索股票代码' if language == 'zh' else 'Search stock code'}" aria-controls="stock-search-suggestions" aria-expanded="false" required>
          <button type="submit" aria-label="{'搜索' if language == 'zh' else 'Search'}" title="{'搜索' if language == 'zh' else 'Search'}">
            <svg viewBox="0 0 24 24" fill="none" aria-hidden="true"><circle cx="10.8" cy="10.8" r="6.3" stroke="currentColor" stroke-width="2"/><path d="m16 16 4.2 4.2" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>
          </button>
          <div class="stock-suggestions" id="stock-search-suggestions" role="listbox" hidden>{stock_search_options}</div>
        </form>
  </div></header>
  <main>
    <header class="report-heading">
      <div><div class="company">{escape(stock_name)} <span class="ticker">{escape(stock_code)}</span></div>
        <h1>{escape(stock_name)} <span class="heading-stock-code">({escape(stock_code)})</span> {escape(localized_title)}</h1>
      </div>
      <div class="coverage" style="--range-max: {max(0, len(dates) - 1)}">
        <div class="coverage-heading"><span>{'报告期范围' if language == 'zh' else 'Reporting range'}</span><strong class="range-output">{escape(dates[-1])} – {escape(dates[0])}</strong></div>
        <div class="range-slider" role="group" aria-label="{'选择报告期范围' if language == 'zh' else 'Select reporting date range'}">
          <div class="range-track"><span></span></div>
          <input class="range-input range-start" type="range" min="0" max="{max(0, len(dates) - 1)}" value="0" aria-label="{'开始报告期' if language == 'zh' else 'Start reporting date'}">
          <input class="range-input range-end" type="range" min="0" max="{max(0, len(dates) - 1)}" value="{max(0, len(dates) - 1)}" aria-label="{'结束报告期' if language == 'zh' else 'End reporting date'}">
        </div>
        <div class="range-endpoints"><span>{escape(dates[-1])}</span><span>{escape(dates[0])}</span></div>
      </div>
    </header>
  <div class="statement-navigation">
    <nav class="statement-tabs" aria-label="{escape(statement_tabs_label)}">{statement_tab_links}</nav>
    <div class="filter-row">
      <div class="period-filters" role="group" aria-label="{escape(period_filters_label)}">{period_filter_buttons}</div>
      <div class="report-tools">
        <label class="amount-unit-control" for="amount-unit"><span>{escape(amount_unit_label)}</span>
          <select id="amount-unit">{amount_unit_options}</select>
        </label>
        <label class="decimal-toggle"><input id="show-decimals" type="checkbox" checked> {escape(show_decimals_label)}</label>
      </div>
    </div>
  </div>
  <div class="table-wrap" role="region" tabindex="0" aria-label="{escape(localized_title)}"><table data-date-count="{len(dates)}" style="--date-column-count: {len(dates)}; --date-column-min-width: 110px">
    <colgroup><col class="item-name-column">{''.join(f'<col class="amount-column" data-reporting-date="{escape(date, quote=True)}">' for date in dates)}</colgroup>
    <thead><tr><th scope="col" class="table-caption-cell"><span>{table_item_label}</span><button type="button" class="hierarchy-toggle-all" data-hierarchy-toggle-all aria-expanded="false">{escape("全部展开" if language == "zh" else "Expand all")}</button></th>{''.join(f'<th scope="col" data-reporting-date="{escape(date)}">{escape(reporting_date_caption(date))}</th>' for date in dates)}</tr></thead>
    <tbody>{''.join(rows)}</tbody>
  </table></div>
  {f'<section class="table-note"><h2>{"说明" if language == "zh" else "Note"}</h2>{table_notes_markup}</section>' if table_notes else ''}
  <div class="trend-modal" id="trend-modal" hidden aria-hidden="true">
    <div class="trend-modal-backdrop" data-trend-modal-close></div>
    <div class="trend-modal-dialog" role="dialog" aria-modal="true" aria-label="{escape('趋势图' if language == 'zh' else 'Trend chart', quote=True)}">
      <button type="button" class="trend-modal-close" data-trend-modal-close aria-label="{escape('关闭趋势图' if language == 'zh' else 'Close trend chart', quote=True)}" title="{escape('关闭趋势图' if language == 'zh' else 'Close trend chart', quote=True)}">×</button>
      <div id="trend-modal-chart-host"></div>
    </div>
  </div>
  <details class="equation-disclaimer">
    <summary>{escape(equation_disclaimer_title)}</summary>
    <p>{escape(equation_disclaimer_intro)}</p>
    <ul>{equation_markup}</ul>
  </details>
  <nav class="recent-companies" aria-label="{'浏览过的公司' if language == 'zh' else 'Recently viewed companies'}" hidden>
    <strong>{'浏览过的公司' if language == 'zh' else 'Recently viewed companies'}</strong>
    <div class="recent-links"></div>
  </nav>
</main>
<script>
  const units = {json.dumps(PUBLIC_AMOUNT_UNITS, ensure_ascii=False)};
  const datesAscending = {json.dumps(list(reversed(dates)), ensure_ascii=False)};
  const unitSelect = document.getElementById('amount-unit');
  const decimalsToggle = document.getElementById('show-decimals');
  const periodButtons = document.querySelectorAll('.period-filters button');
  const rangeStartInput = document.querySelector('.range-start');
  const rangeEndInput = document.querySelector('.range-end');
  const rangeOutput = document.querySelector('.range-output');
  const rangeTrack = document.querySelector('.range-track span');
  let activePeriodEnding = '';
  const itemRows = document.querySelectorAll('[data-item-id]');
  const itemToggles = document.querySelectorAll('[data-item-toggle]');
  const hierarchyToggleAll = document.querySelector('[data-hierarchy-toggle-all]');
  const expandAllLabel = {json.dumps("全部展开" if language == "zh" else "Expand all", ensure_ascii=False)};
  const collapseAllLabel = {json.dumps("全部收起" if language == "zh" else "Collapse all", ensure_ascii=False)};
  const amountUnitPreferenceKey = 'easy10k.amountUnit';
  const showDecimalsPreferenceKey = 'easy10k.showDecimals';
  const periodEndingPreferenceKey = 'easy10k.periodEnding';
  const statementScrollPreferenceKey = 'easy10k.statementScrollY';
  const recentStocksPreferenceKey = 'easy10k.recentStocks';
  const stockDirectory = {stock_directory_json};
  const currentStockCode = '{escape(stock_code, quote=True)}';
  function loadRecentStocks() {{
    try {{
      const stored = JSON.parse(localStorage.getItem(recentStocksPreferenceKey) || '[]');
      return Array.isArray(stored) ? stored.filter((code) => Object.prototype.hasOwnProperty.call(stockDirectory, code)) : [];
    }} catch (_) {{
      return [];
    }}
  }}
  function renderRecentStocks(grid, stocks) {{
    grid.replaceChildren();
    stocks.slice(0, 12).forEach((code) => {{
      const link = document.createElement('a');
      link.className = 'company-link';
      link.href = `/stocks/${{encodeURIComponent(code)}}/{statement_slug}?lang={language}`;
      link.title = code;
      link.setAttribute('aria-label', `${{stockDirectory[code]}} (${{code}})`);
      const nameLabel = document.createElement('strong');
      nameLabel.textContent = stockDirectory[code];
      link.append(nameLabel);
      grid.append(link);
    }});
  }}
  const recentStocks = loadRecentStocks().filter((code) => code !== currentStockCode);
  recentStocks.unshift(currentStockCode);
  localStorage.setItem(recentStocksPreferenceKey, JSON.stringify(recentStocks.slice(0, 10)));
  const recentCompanies = document.querySelector('.recent-companies');
  renderRecentStocks(recentCompanies.querySelector('.recent-links'), recentStocks.slice(1));
  recentCompanies.hidden = recentStocks.length <= 1;
  const stockSearch = document.getElementById('stock-search');
  const stockSearchCode = document.getElementById('stock-search-code');
  const stockSuggestions = document.getElementById('stock-search-suggestions');
  const stockSuggestionButtons = Array.from(stockSuggestions.querySelectorAll('.stock-suggestion'));
  function renderStockSuggestions() {{
    const query = stockSearchCode.value.trim().toLowerCase();
    let visible = 0;
    stockSuggestionButtons.forEach((suggestion) => {{
      const matches = !query || suggestion.dataset.stockCode.includes(query) || suggestion.dataset.stockName.toLowerCase().includes(query);
      suggestion.hidden = !matches;
      if (matches) visible += 1;
    }});
    stockSuggestions.hidden = visible === 0;
    stockSearchCode.setAttribute('aria-expanded', String(visible > 0));
  }}
  stockSearchCode.addEventListener('focus', renderStockSuggestions);
  stockSearchCode.addEventListener('input', renderStockSuggestions);
  stockSuggestionButtons.forEach((suggestion) => {{
    suggestion.addEventListener('click', () => {{
      stockSearchCode.value = suggestion.dataset.stockCode;
      stockSuggestions.hidden = true;
      stockSearchCode.setAttribute('aria-expanded', 'false');
      stockSearchCode.focus();
    }});
  }});
  document.addEventListener('click', (event) => {{
    if (!stockSearch.contains(event.target)) {{
      stockSuggestions.hidden = true;
      stockSearchCode.setAttribute('aria-expanded', 'false');
    }}
  }});
  stockSearch.addEventListener('submit', (event) => {{
    event.preventDefault();
    const code = stockSearchCode.value.trim();
    if (!/^[0-9]{{6}}$/.test(code)) {{
      stockSearchCode.reportValidity();
      return;
    }}
    window.location.href = `/stocks/${{encodeURIComponent(code)}}/balance-sheet?lang={language}`;
  }});
  if ('scrollRestoration' in history) history.scrollRestoration = 'manual';
  function resizeDateColumns() {{
    const table = document.querySelector('.table-wrap table');
    if (!table) return;
    const count = table.querySelectorAll('col.amount-column:not([hidden])').length;
    if (!count) return;
    const minimumWidth = Number.parseFloat(getComputedStyle(table).getPropertyValue('--date-column-min-width')) || 120;
    const labelWidth = table.querySelector('thead th').getBoundingClientRect().width;
    const availableWidth = Math.max(table.parentElement.clientWidth, labelWidth + count * minimumWidth);
    const dateColumnWidth = (availableWidth - labelWidth) / count;
    table.style.width = '100%';
    table.style.minWidth = `${{availableWidth}}px`;
    table.querySelectorAll('col.amount-column').forEach((column) => {{
      column.style.width = `${{dateColumnWidth}}px`;
    }});
  }}
  function renderPeriods(ending) {{
    activePeriodEnding = ending;
    const startIndex = Number(rangeStartInput.value);
    const endIndex = Number(rangeEndInput.value);
    const rangeStart = datesAscending[startIndex];
    const rangeEnd = datesAscending[endIndex];
    rangeOutput.textContent = `${{rangeStart}} – ${{rangeEnd}}`;
    const lastIndex = Math.max(1, datesAscending.length - 1);
    rangeTrack.style.left = `${{startIndex / lastIndex * 100}}%`;
    rangeTrack.style.right = `${{(datesAscending.length - 1 - endIndex) / lastIndex * 100}}%`;
    document.querySelectorAll('[data-reporting-date]').forEach((cell) => {{
      const date = cell.dataset.reportingDate;
      cell.hidden = date < rangeStart || date > rangeEnd || (Boolean(ending) && !date.endsWith(ending));
    }});
    periodButtons.forEach((button) => {{
      const active = button.dataset.periodEnding === ending;
      button.classList.toggle('active', active);
      button.setAttribute('aria-pressed', String(active));
    }});
    resizeDateColumns();
    renderExpandedTrendCharts();
  }}
  function renderItemHierarchy() {{
    itemRows.forEach((row) => {{
      if (row.classList.contains('other-profit-loss-detail')) {{
        let parentId = Number(row.dataset.parentId || Math.floor(Number(row.dataset.itemId) / 100));
        let hidden = !otherProfitLossExpanded;
        while (!hidden && parentId > 0) {{
          if (collapsedItems.has(parentId)) {{
            hidden = true;
            break;
          }}
          parentId = Math.floor(parentId / 100);
        }}
        row.hidden = hidden;
        return;
      }}
      let parentId = Number(row.dataset.parentId || Math.floor(Number(row.dataset.itemId) / 100));
      let hidden = false;
      while (parentId > 0) {{
        if (collapsedItems.has(parentId)) {{
          hidden = true;
          break;
        }}
        parentId = Math.floor(parentId / 100);
      }}
      row.hidden = hidden;
    }});
    itemToggles.forEach((toggle) => {{
      const itemId = Number(toggle.dataset.itemToggle);
      const collapsed = collapsedItems.has(itemId);
      toggle.setAttribute('aria-expanded', String(!collapsed));
    }});
    const allExpanded = Array.from(itemToggles).every((toggle) =>
      !collapsedItems.has(Number(toggle.dataset.itemToggle))
    ) && (!otherProfitLossToggle || otherProfitLossExpanded);
    if (hierarchyToggleAll) {{
      hierarchyToggleAll.textContent = allExpanded ? collapseAllLabel : expandAllLabel;
      hierarchyToggleAll.setAttribute('aria-expanded', String(allExpanded));
    }}
  }}
  const collapsedItems = new Set({json.dumps([82, 83] if statement == "cash_flow_statement" else [44, 56, 57, 58, 4225] if statement == "income_statement" else [110128, 1102, 21, 31] if statement == "balance_sheet" else [])});
  let otherProfitLossExpanded = false;
  const otherProfitLossToggle = document.querySelector('[data-other-profit-loss-toggle]');
  hierarchyToggleAll?.addEventListener('click', () => {{
    const allExpanded = Array.from(itemToggles).every((toggle) =>
      !collapsedItems.has(Number(toggle.dataset.itemToggle))
    ) && (!otherProfitLossToggle || otherProfitLossExpanded);
    if (allExpanded) {{
      itemToggles.forEach((toggle) => collapsedItems.add(Number(toggle.dataset.itemToggle)));
      otherProfitLossExpanded = false;
    }} else {{
      collapsedItems.clear();
      otherProfitLossExpanded = true;
    }}
    if (otherProfitLossToggle) {{
      otherProfitLossToggle.setAttribute('aria-expanded', String(otherProfitLossExpanded));
    }}
    renderItemHierarchy();
  }});
  otherProfitLossToggle?.addEventListener('click', () => {{
    otherProfitLossExpanded = !otherProfitLossExpanded;
    otherProfitLossToggle.setAttribute('aria-expanded', String(otherProfitLossExpanded));
    renderItemHierarchy();
  }});
  itemToggles.forEach((toggle) => {{
    toggle.addEventListener('click', () => {{
      const itemId = Number(toggle.dataset.itemToggle);
      if (collapsedItems.has(itemId)) collapsedItems.delete(itemId);
      else collapsedItems.add(itemId);
      renderItemHierarchy();
    }});
  }});
  const trendToggles = document.querySelectorAll('[data-trend-toggle]');
  const trendModal = document.getElementById('trend-modal');
  const trendModalChartHost = document.getElementById('trend-modal-chart-host');
  const trendModalCloseButtons = document.querySelectorAll('[data-trend-modal-close]');
  let activeTrendItemId = null;
  const originalTrendParents = new Map();
  const svgNamespace = 'http://www.w3.org/2000/svg';
  function appendSvgElement(svg, tagName, attributes, textContent) {{
    const element = document.createElementNS(svgNamespace, tagName);
    Object.entries(attributes).forEach(([name, value]) => element.setAttribute(name, String(value)));
    if (textContent !== undefined) element.textContent = textContent;
    svg.append(element);
    return element;
  }}
  function renderTrendChart(itemId) {{
    const row = document.querySelector('tr[data-item-id="' + itemId + '"]');
    const svg = document.querySelector('[data-trend-chart="' + itemId + '"]');
    if (!row || !svg) return;
    const cells = Array.from(row.querySelectorAll('td.amount-column')).filter((cell) => !cell.hidden).reverse();
    const dates = cells.map((cell) => cell.dataset.reportingDate || '');
    const values = cells.map((cell) => {{
      const raw = Number(cell.dataset.amount);
      return Number.isFinite(raw) && cell.dataset.amount !== '' ? raw : null;
    }});
    const grossMargins = cells.map((cell) => {{
      const raw = Number(cell.dataset.grossMargin);
      return Number.isFinite(raw) && cell.dataset.grossMargin !== '' ? raw : null;
    }});
    const valid = values.filter((value) => value !== null);
    svg.replaceChildren();
    if (!valid.length) {{
      appendSvgElement(svg, 'text', {{ x: 32, y: 32 }}, '—');
      return;
    }}
    const perShare = cells.some((cell) => cell.dataset.perShare === 'true');
    const unit = perShare ? '元/股' : unitSelect.value;
    const showDecimals = decimalsToggle.checked;
    const isGrossProfit = itemId === '{CALCULATED_GROSS_PROFIT_ID}';
    const isNetProfit = itemId === '56';
    const isOperatingProfit = itemId === '51';
    const hasMarginSeries = isGrossProfit || isNetProfit || isOperatingProfit;
    const width = 1075;
    const height = 320;
    const padding = 42;
    const plotTop = 58;
    const bottom = height - 56;
    const axisRight = width - 24;
    const scaleFactor = perShare ? 1 : units[unit];
    const dataMin = Math.min(0, ...valid) / scaleFactor;
    const dataMax = Math.max(0, ...valid) / scaleFactor;
    const dataRange = dataMax - dataMin || 1;
    const rawTickStep = dataRange / 5;
    const tickMagnitude = 10 ** Math.floor(Math.log10(rawTickStep));
    const normalizedTickStep = rawTickStep / tickMagnitude;
    const niceTickMultiplier = normalizedTickStep <= 1 ? 1 : normalizedTickStep <= 2 ? 2 : normalizedTickStep <= 2.5 ? 2.5 : normalizedTickStep <= 5 ? 5 : 10;
    let tickStep = niceTickMultiplier * tickMagnitude;
    let axisMinDisplay = Math.floor(dataMin / tickStep) * tickStep;
    let axisMaxDisplay = Math.ceil(dataMax / tickStep - 1e-12) * tickStep;
    while (Math.round((axisMaxDisplay - axisMinDisplay) / tickStep) + 1 > 6) {{
      const stepMagnitude = 10 ** Math.floor(Math.log10(tickStep));
      const normalizedStep = tickStep / stepMagnitude;
      tickStep = (normalizedStep < 1.5 ? 2 : normalizedStep < 3.5 ? 5 : 10) * stepMagnitude;
      axisMinDisplay = Math.floor(dataMin / tickStep) * tickStep;
      axisMaxDisplay = Math.ceil(dataMax / tickStep - 1e-12) * tickStep;
    }}
    const axisMin = axisMinDisplay * scaleFactor;
    const axisMax = axisMaxDisplay * scaleFactor;
    const range = axisMax - axisMin || scaleFactor;
    const yFor = (value) => bottom - ((value - axisMin) / range) * (bottom - plotTop);
    const spacing = (width - padding * 2) / Math.max(values.length, 1);
    const zeroY = yFor(0);
    const formatValue = (value) => {{
      const displayValue = value / (perShare ? 1 : units[unit]);
      return displayValue.toLocaleString(undefined, {{
        minimumFractionDigits: perShare || showDecimals ? 2 : 0,
        maximumFractionDigits: perShare ? 4 : showDecimals ? 2 : 0,
      }});
    }};
    const chartWrap = svg.closest('.trend-chart-wrap');
    chartWrap?.querySelector('.trend-hover-tooltip')?.remove();
    const tooltip = document.createElement('div');
    tooltip.className = 'trend-hover-tooltip';
    tooltip.setAttribute('role', 'tooltip');
    const tooltipValue = document.createElement('span');
    tooltipValue.className = 'trend-hover-tooltip-value';
    const tooltipDate = document.createElement('span');
    tooltipDate.className = 'trend-hover-tooltip-date';
    const tooltipMargin = document.createElement('span');
    tooltipMargin.className = 'trend-hover-tooltip-margin';
    if (hasMarginSeries) tooltip.append(tooltipValue, tooltipDate, tooltipMargin);
    else tooltip.append(tooltipValue, tooltipDate);
    chartWrap?.append(tooltip);
    const hideTrendTooltip = () => tooltip.classList.remove('visible');
    const showTrendTooltip = (pointElement, value, date, marginOnly = false) => {{
      if (!chartWrap) return;
      tooltipValue.textContent = marginOnly ? value.toFixed(2) + '%' : formatValue(value) + ' ' + unit;
      tooltipDate.textContent = date;
      const pointIndex = dates.indexOf(date);
      if (!marginOnly && hasMarginSeries && grossMargins[pointIndex] !== null) {{
        tooltipMargin.textContent = grossMargins[pointIndex].toFixed(2) + '%';
        tooltipValue.append(' ', tooltipMargin);
      }}
      tooltip.classList.add('visible');
      const pointBox = pointElement.getBoundingClientRect();
      const wrapBox = chartWrap.getBoundingClientRect();
      const halfWidth = tooltip.offsetWidth / 2;
      const centerX = pointBox.left - wrapBox.left + pointBox.width / 2;
      tooltip.style.left = Math.max(halfWidth + 8, Math.min(wrapBox.width - halfWidth - 8, centerX)) + 'px';
      tooltip.style.top = (pointBox.top - wrapBox.top) + 'px';
    }};
    const trendStockLabel = svg.dataset.trendStock || '';
    appendSvgElement(svg, 'text', {{ class: 'trend-chart-title', x: padding, y: 24 }}, trendStockLabel + ' · ' + (svg.dataset.trendLabel || ''));
    appendSvgElement(svg, 'text', {{ class: 'trend-chart-unit', x: padding, y: 43, 'text-anchor': 'start' }}, '单位：' + unit);
    appendSvgElement(svg, 'line', {{ class: 'trend-axis', x1: padding, x2: padding, y1: plotTop, y2: bottom }});
    appendSvgElement(svg, 'line', {{ class: 'trend-axis', x1: padding, x2: axisRight, y1: zeroY, y2: zeroY }});
    const tickCount = Math.round((axisMaxDisplay - axisMinDisplay) / tickStep) + 1;
    for (let tickIndex = 0; tickIndex < tickCount; tickIndex += 1) {{
      const tickValue = axisMinDisplay + tickIndex * tickStep;
      const gridValue = tickValue * scaleFactor;
      const gridY = yFor(gridValue);
      if (Math.abs(gridY - zeroY) > 1) appendSvgElement(svg, 'line', {{ class: 'trend-grid', x1: padding, x2: axisRight, y1: gridY, y2: gridY }});
      appendSvgElement(svg, 'text', {{ x: padding - 10, y: gridY + 4, 'text-anchor': 'end' }}, tickValue.toLocaleString(undefined, {{ maximumFractionDigits: tickStep < 1 ? 2 : 0 }}));
    }}
    const points = values.map((value, index) => value === null ? null : ({{ x: padding + (index + 0.5) * spacing, y: yFor(value) }}));
    const linePoints = points.filter((point) => point !== null);
    if (linePoints.length > 1) {{
      const linePath = linePoints.map((point, index) => (index === 0 ? 'M' : 'L') + ' ' + point.x + ' ' + point.y).join(' ');
      const areaPath = linePath + ' L ' + linePoints[linePoints.length - 1].x + ' ' + zeroY + ' L ' + linePoints[0].x + ' ' + zeroY + ' Z';
      const defs = appendSvgElement(svg, 'defs', {{}});
      const gradient = appendSvgElement(defs, 'linearGradient', {{ id: 'trend-gradient-' + itemId, x1: '0', x2: '0', y1: '0', y2: '1' }});
      appendSvgElement(gradient, 'stop', {{ offset: '0%', 'stop-color': 'var(--accent)', 'stop-opacity': '.24' }});
      appendSvgElement(gradient, 'stop', {{ offset: '100%', 'stop-color': 'var(--accent)', 'stop-opacity': '.03' }});
      appendSvgElement(svg, 'path', {{ class: 'trend-area', d: areaPath, fill: 'url(#trend-gradient-' + itemId + ')' }});
      appendSvgElement(svg, 'path', {{ class: 'trend-line', d: linePath }});
    }}
    let marginPoints = [];
    if (hasMarginSeries) {{
      const validGrossMargins = grossMargins.filter((margin) => margin !== null);
      const observedMarginMax = validGrossMargins.length ? Math.max(...validGrossMargins) : 0;
      const marginAxisMax = Math.max(20, Math.ceil(observedMarginMax / 5) * 5);
      const marginYFor = (margin) => bottom - (Math.max(0, Math.min(marginAxisMax, margin)) / marginAxisMax) * (bottom - plotTop);
      appendSvgElement(svg, 'line', {{ class: 'trend-margin-axis', x1: axisRight, x2: axisRight, y1: plotTop, y2: bottom }});
      [0, marginAxisMax / 2, marginAxisMax].forEach((margin) => appendSvgElement(svg, 'text', {{ class: 'trend-margin-label', x: axisRight + 10, y: marginYFor(margin) + 4, 'text-anchor': 'start' }}, margin + '%'));
      const marginLabel = isNetProfit
        ? '{escape("净利率" if language == "zh" else "Net margin")}'
        : isOperatingProfit
          ? '{escape("营业利润率" if language == "zh" else "Operating margin")}'
          : '{escape("毛利率" if language == "zh" else "Gross margin")}';
      appendSvgElement(svg, 'text', {{ class: 'trend-margin-label', x: axisRight, y: 43, 'text-anchor': 'end' }}, marginLabel);
      marginPoints = grossMargins.map((margin, index) => margin === null ? null : ({{ x: padding + (index + 0.5) * spacing, y: marginYFor(margin) }}));
      const marginLinePoints = marginPoints.filter((point) => point !== null);
      if (marginLinePoints.length > 1) {{
        const marginLinePath = marginLinePoints.map((point, index) => (index === 0 ? 'M' : 'L') + ' ' + point.x + ' ' + point.y).join(' ');
        appendSvgElement(svg, 'path', {{ class: 'trend-margin-line', d: marginLinePath }});
      }}
      marginPoints.forEach((point, index) => {{
        if (point === null) return;
        const marginPointElement = appendSvgElement(svg, 'circle', {{ class: 'trend-margin-point', cx: point.x, cy: point.y, r: 3, tabindex: '0', 'aria-label': dates[index] + ': ' + grossMargins[index].toFixed(2) + '%' }});
        marginPointElement.addEventListener('mouseenter', () => showTrendTooltip(marginPointElement, values[index], dates[index]));
        marginPointElement.addEventListener('mouseleave', hideTrendTooltip);
        marginPointElement.addEventListener('focus', () => showTrendTooltip(marginPointElement, values[index], dates[index]));
        marginPointElement.addEventListener('blur', hideTrendTooltip);
      }});
    }}
    let latestIndex = -1;
    values.forEach((value, index) => {{ if (value !== null) latestIndex = index; }});
    const visiblePointLabelIndices = new Set();
    const visibleDateLabelIndices = new Set();
    const validPointCount = values.filter((value) => value !== null).length;
    if (validPointCount <= 12) {{
      values.forEach((value, index) => {{
        visibleDateLabelIndices.add(index);
        if (value !== null) visiblePointLabelIndices.add(index);
      }});
    }} else {{
      let pointsFromLatest = 0;
      for (let index = latestIndex; index >= 0; index -= 1) {{
        if (values[index] === null) continue;
        if (pointsFromLatest % 4 === 0) {{
          visiblePointLabelIndices.add(index);
          visibleDateLabelIndices.add(index);
        }}
        pointsFromLatest += 1;
      }}
    }}
    const pointLabelBounds = [];
    const amountLabelsByIndex = new Map();
    values.forEach((value, index) => {{
      const point = points[index];
      if (visibleDateLabelIndices.has(index)) appendSvgElement(svg, 'text', {{ x: padding + (index + 0.5) * spacing, y: bottom + 22, 'text-anchor': 'middle' }}, dates[index].slice(2).replaceAll('-', '/'));
      if (value === null) return;
      const pointElement = appendSvgElement(svg, 'circle', {{ class: 'trend-point', cx: point.x, cy: point.y, r: 3.5, tabindex: '0', 'aria-label': dates[index] + ': ' + formatValue(value) + ' ' + unit }});
      pointElement.addEventListener('mouseenter', () => showTrendTooltip(pointElement, value, dates[index]));
      pointElement.addEventListener('mouseleave', hideTrendTooltip);
      pointElement.addEventListener('focus', () => showTrendTooltip(pointElement, value, dates[index]));
      pointElement.addEventListener('blur', hideTrendTooltip);
      if (visiblePointLabelIndices.has(index)) {{
        const valueLabel = appendSvgElement(svg, 'text', {{ class: 'trend-point-label', x: point.x, y: point.y - 14, 'text-anchor': 'middle' }}, formatValue(value));
        amountLabelsByIndex.set(index, {{ element: valueLabel, boundsIndex: pointLabelBounds.length }});
        pointLabelBounds.push(valueLabel.getBBox());
      }}
    }});
    if (hasMarginSeries) {{
      const occupiedLabelBounds = [...pointLabelBounds];
      const marginLabelsBelowPoint = new Set();
      const overlapsLabel = (candidate) => occupiedLabelBounds.some((occupied) =>
        candidate.x < occupied.x + occupied.width + 3
        && candidate.x + candidate.width + 3 > occupied.x
        && candidate.y < occupied.y + occupied.height + 3
        && candidate.y + candidate.height + 3 > occupied.y
      );
      grossMargins.forEach((margin, index) => {{
        const point = marginPoints[index];
        if (margin === null || point === null || !visiblePointLabelIndices.has(index)) return;
        const label = appendSvgElement(svg, 'text', {{ class: 'trend-margin-label', x: point.x, y: point.y - 8, 'text-anchor': 'middle' }}, margin.toFixed(2) + '%');
        const amountLabelEntry = amountLabelsByIndex.get(index);
        const amountLabel = amountLabelEntry?.element;
        const amountPoint = points[index];
        if (amountLabel && amountPoint) {{
          const marginLabelBounds = label.getBBox();
          const amountLabelBounds = amountLabel.getBBox();
          const labelsOverlap = marginLabelBounds.x < amountLabelBounds.x + amountLabelBounds.width + 3
            && marginLabelBounds.x + marginLabelBounds.width + 3 > amountLabelBounds.x
            && marginLabelBounds.y < amountLabelBounds.y + amountLabelBounds.height + 3
            && marginLabelBounds.y + marginLabelBounds.height + 3 > amountLabelBounds.y;
          const pointsClose = Math.abs(point.x - amountPoint.x) < 32 && Math.abs(point.y - amountPoint.y) < 36;
          if ((labelsOverlap || pointsClose) && point.y > amountPoint.y) {{
            marginLabelsBelowPoint.add(index);
          }} else if (labelsOverlap || pointsClose) {{
            amountLabel.setAttribute('y', amountPoint.y + 18);
            const updatedBounds = amountLabel.getBBox();
            pointLabelBounds[amountLabelEntry.boundsIndex] = updatedBounds;
            occupiedLabelBounds[amountLabelEntry.boundsIndex] = updatedBounds;
          }}
        }}
        if (marginLabelsBelowPoint.has(index)) {{
          label.setAttribute('y', point.y + 18);
          occupiedLabelBounds.push(label.getBBox());
          return;
        }}
        let placed = false;
        const horizontalOffsets = [0, -10, 10, -20, 20];
        for (let lift = 0; lift <= 56 && !placed; lift += 14) {{
          for (const offset of horizontalOffsets) {{
            const x = point.x + offset;
            const y = point.y - 8 - lift;
            label.setAttribute('x', x);
            label.setAttribute('y', y);
            const bounds = label.getBBox();
            const insidePlot = bounds.x >= padding - 2 && bounds.x + bounds.width <= axisRight + 2;
            if (insidePlot && bounds.y >= plotTop - 2 && !overlapsLabel(bounds)) {{
              occupiedLabelBounds.push(bounds);
              placed = true;
              break;
            }}
          }}
        }}
        if (!placed) {{
          label.setAttribute('x', point.x);
          label.setAttribute('y', point.y - 8);
        }}
      }});
    }}
  }}
  function renderExpandedTrendCharts() {{
    if (activeTrendItemId !== null) renderTrendChart(activeTrendItemId);
  }}
  function setTrendExpanded(itemId, expanded) {{
    const toggle = document.querySelector('[data-trend-toggle="' + itemId + '"]');
    const detail = document.querySelector('[data-trend-item-id="' + itemId + '"]');
    const detailContainer = document.querySelector('[data-trend-detail="' + itemId + '"]');
    if (!toggle || !detail || !detailContainer || !trendModal || !trendModalChartHost) return;
    if (expanded) {{
      if (activeTrendItemId !== null && activeTrendItemId !== itemId) setTrendExpanded(activeTrendItemId, false);
      originalTrendParents.set(itemId, detailContainer.parentElement);
      trendModalChartHost.append(detailContainer);
      detail.hidden = true;
      trendModal.hidden = false;
      trendModal.setAttribute('aria-hidden', 'false');
      activeTrendItemId = itemId;
    }} else {{
      const originalParent = originalTrendParents.get(itemId);
      if (originalParent) originalParent.append(detailContainer);
      detail.hidden = true;
      if (activeTrendItemId === itemId) activeTrendItemId = null;
      trendModal.hidden = true;
      trendModal.setAttribute('aria-hidden', 'true');
    }}
    toggle.setAttribute('aria-expanded', String(expanded));
    toggle.title = expanded ? '{escape("隐藏趋势" if language == "zh" else "Hide trend")}' : '{escape("查看趋势" if language == "zh" else "Show trend")}';
    toggle.classList.toggle('active', expanded);
    if (expanded) renderTrendChart(itemId);
  }}
  trendToggles.forEach((toggle) => toggle.addEventListener('click', () => {{
    const itemId = toggle.dataset.trendToggle;
    setTrendExpanded(itemId, toggle.getAttribute('aria-expanded') !== 'true');
  }}));
  trendModalCloseButtons.forEach((button) => button.addEventListener('click', () => {{
    if (activeTrendItemId !== null) setTrendExpanded(activeTrendItemId, false);
  }}));
  document.addEventListener('keydown', (event) => {{
    if (event.key === 'Escape' && activeTrendItemId !== null) setTrendExpanded(activeTrendItemId, false);
  }});
  document.addEventListener('click', (event) => {{
    const target = event.target instanceof Element ? event.target : null;
    if (target && (target.closest('.trend-modal-dialog') || target.closest('.trend-chart-wrap') || target.closest('[data-trend-toggle]'))) return;
    if (activeTrendItemId !== null) setTrendExpanded(activeTrendItemId, false);
  }});
  function renderAmounts() {{
    const unit = unitSelect.value;
    const showDecimals = decimalsToggle.checked;
    const dateColumnWidth = showDecimals && unit === '元' ? 140 : (unit === '百万元' || unit === '亿元' ? 110 : 120);
    const table = document.querySelector('.table-wrap table');
    if (table) table.style.setProperty('--date-column-min-width', `${{dateColumnWidth}}px`);
    resizeDateColumns();
    document.querySelectorAll('[data-amount]').forEach((cell) => {{
      const raw = Number(cell.dataset.amount);
      if (!Number.isFinite(raw)) return;
      if (raw === 0) {{
        cell.textContent = cell.dataset.zeroDisplay === 'blank' ? '\u00a0' : '—';
        return;
      }}
      const perShare = cell.dataset.perShare === 'true';
      const value = raw / (perShare ? 1 : units[unit]);
      cell.textContent = value.toLocaleString(undefined, {{
        minimumFractionDigits: showDecimals ? 2 : 0,
        maximumFractionDigits: perShare ? 4 : showDecimals ? 2 : 0,
      }});
    }});
    renderExpandedTrendCharts();
  }}
  periodButtons.forEach((button) => {{
    button.addEventListener('click', () => {{
      const ending = button.dataset.periodEnding;
      localStorage.setItem(periodEndingPreferenceKey, ending);
      renderPeriods(ending);
    }});
  }});
  [rangeStartInput, rangeEndInput].forEach((input) => input.addEventListener('input', () => {{
    if (Number(rangeStartInput.value) > Number(rangeEndInput.value)) {{
      if (input === rangeStartInput) rangeEndInput.value = rangeStartInput.value;
      else rangeStartInput.value = rangeEndInput.value;
    }}
    renderPeriods(activePeriodEnding);
  }}));
  document.querySelectorAll('.statement-tabs a').forEach((link) => {{
    link.addEventListener('click', () => {{
      sessionStorage.setItem(statementScrollPreferenceKey, String(window.scrollY));
    }});
    link.addEventListener('pointerenter', () => {{
      if (navigator.connection && navigator.connection.saveData) return;
      if (link.dataset.prefetched) return;
      link.dataset.prefetched = 'true';
      const prefetch = document.createElement('link');
      prefetch.rel = 'prefetch';
      prefetch.href = link.href;
      document.head.appendChild(prefetch);
    }}, {{ once: true }});
  }});
  function restoreStatementScroll() {{
    const storedScroll = sessionStorage.getItem(statementScrollPreferenceKey);
    if (storedScroll === null) return;
    const scrollY = Number(storedScroll);
    sessionStorage.removeItem(statementScrollPreferenceKey);
    if (!Number.isFinite(scrollY)) return;
    requestAnimationFrame(() => requestAnimationFrame(() => window.scrollTo(0, scrollY)));
  }}
  window.addEventListener('pageshow', restoreStatementScroll);
  restoreStatementScroll();
  unitSelect.addEventListener('change', () => {{
    localStorage.setItem(amountUnitPreferenceKey, unitSelect.value);
    renderAmounts();
  }});
  decimalsToggle.addEventListener('change', () => {{
    localStorage.setItem(showDecimalsPreferenceKey, String(decimalsToggle.checked));
    renderAmounts();
  }});
  const storedUnit = localStorage.getItem(amountUnitPreferenceKey);
  if (storedUnit && Object.prototype.hasOwnProperty.call(units, storedUnit)) {{
    unitSelect.value = storedUnit;
  }}
  const storedDecimals = localStorage.getItem(showDecimalsPreferenceKey);
  if (storedDecimals === 'true' || storedDecimals === 'false') {{
    decimalsToggle.checked = storedDecimals === 'true';
  }}
  const storedEnding = localStorage.getItem(periodEndingPreferenceKey);
  const validEnding = Array.from(periodButtons).some((button) => button.dataset.periodEnding === storedEnding);
  renderPeriods(validEnding ? storedEnding : '');
  renderItemHierarchy();
  renderAmounts();
  window.addEventListener('resize', () => {{
    resizeDateColumns();
  }});
</script>
</body></html>"""
    return HTMLResponse(html, headers={"X-Content-Type-Options": "nosniff"})

@app.get("/line-item-chart")
def get_line_item_chart() -> list[dict[str, int | str | None]]:
    """Return the financial statement line-item chart as JSON."""
    return [
        {
            "item_id": item.item_id,
            "name_cn": item.name,
            "name_en": item.english_name,
        }

        for item in line_item_chart._items_by_id.values()
    ]

@app.get("/balance_sheet_items")
def get_balance_sheet_stock_codes() -> list[dict]:
    """List stocks that have stored balance-sheet items and their report counts."""
    with LineItemStorage() as storage:
        if not storage.database_url:
            raise HTTPException(status_code=503, detail="DATABASE_URL is not configured")
        try:
            return storage.get_balance_sheet_stock_codes()
        except Exception as exc:
            logger.warning("Unable to fetch balance-sheet stock codes", exc_info=True)
            raise HTTPException(status_code=503, detail="Unable to fetch stock codes") from exc


@app.get("/balance_sheet_items/{stock_code}")
def get_balance_sheet_items(stock_code: str) -> list[dict]:
    """Return all balance-sheet items stored for a stock code."""
    storage = LineItemStorage()
    if not storage.database_url:
        logger.warning(f"DATABASE_URL is not configured")
        raise HTTPException(status_code=503, detail="DATABASE_URL is not configured")

    logger.info(f"get_balance_sheet_items {stock_code}")
    try:
        with storage:
            return storage.get_balance_sheet_items(stock_code)
    except Exception as exc:
        logger.warning(f"database connect error:{exc}")
        raise HTTPException(
            status_code=503,
            detail="Unable to fetch balance-sheet items",
        ) from exc

@app.get("/income_statement_items")
def get_income_statement_stock_codes() -> list[dict]:
    return get_statement_stock_codes("income_statement")


@app.get("/cash_flow_statement_items")
def get_cash_flow_statement_stock_codes() -> list[dict]:
    return get_statement_stock_codes("cash_flow_statement")


def get_statement_stock_codes(statement: str) -> list[dict]:
    with LineItemStorage() as storage:
        if not storage.database_url:
            raise HTTPException(status_code=503, detail="DATABASE_URL is not configured")
        try:
            return storage.get_statement_stock_codes(statement)
        except Exception as exc:
            logger.warning("Unable to fetch statement stock codes", exc_info=True)
            raise HTTPException(status_code=503, detail="Unable to fetch stock codes") from exc


@app.get("/income_statement_items/{stock_code}")
def get_income_statement_items(stock_code: str) -> list[dict]:
    return get_statement_items("income_statement", stock_code)


@app.get("/cash_flow_statement_items/{stock_code}")
def get_cash_flow_statement_items(stock_code: str) -> list[dict]:
    return get_statement_items("cash_flow_statement", stock_code)


def get_statement_items(statement: str, stock_code: str) -> list[dict]:
    with LineItemStorage() as storage:
        if not storage.database_url:
            raise HTTPException(status_code=503, detail="DATABASE_URL is not configured")
        try:
            return storage.get_statement_items(statement, stock_code)
        except Exception as exc:
            logger.warning("Unable to fetch statement items", exc_info=True)
            raise HTTPException(status_code=503, detail="Unable to fetch statement items") from exc


def source_pdf_path(local_path):
    if not local_path:
        raise HTTPException(status_code=404, detail="Source PDF is unavailable")
    path = (PROJECT_DIR / local_path).resolve()
    if not path.is_relative_to((PROJECT_DIR / "reports").resolve()):
        raise HTTPException(status_code=403, detail="Source is outside report directories")
    if path.suffix.lower() != ".pdf" or not path.is_file():
        raise HTTPException(status_code=404, detail="Source PDF is unavailable")
    return path


@app.get("/balance_sheet_items/{stock_code}/{reporting_date}/{item_id}/source")
def get_item_source(stock_code: str, reporting_date: str, item_id: int,
                    report_creation_time: str = Query(min_length=1)):
    with LineItemStorage() as storage:
        if not storage.database_url:
            raise HTTPException(status_code=503, detail="DATABASE_URL is not configured")
        try:
            record = storage.get_balance_sheet_source(stock_code, reporting_date, item_id, report_creation_time)
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except Exception as error:
            logger.warning("Unable to resolve source report", exc_info=True)
            raise HTTPException(status_code=503, detail="Unable to resolve source report") from error
    if not record:
        raise HTTPException(status_code=404, detail="No source PDF registered for this report version")
    download_url = record.get("download_url")
    parsed_url = urlsplit(download_url or "")
    if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
        download_url = None
    fallback = {"pdf_hash": record["pdf_hash"], "download_url": download_url}
    try:
        path = source_pdf_path(record["local_path"])
        source = locate_source(record, item_id, path, SOURCE_CACHE_DIR)
    except HTTPException as error:
        raise HTTPException(status_code=error.status_code, detail={"message": error.detail, **fallback}) from error
    except (OSError, ValueError, RuntimeError) as error:
        raise HTTPException(status_code=409, detail={"message": str(error), **fallback}) from error
    page_url = f"/report-source/{source['pdf_hash']}/page/{source['page_number']}.png"
    source["page_image_url"] = page_url
    source["image_url"] = f"{page_url}?top={source['excerpt_top']}&bottom={source['excerpt_bottom']}"
    source["download_url"] = download_url
    return source


@app.get("/report-source/{pdf_hash}/page/{page_number}.png")
def get_source_image(pdf_hash: str, page_number: int,
                     width: int = Query(default=960, ge=960, le=1600),
                     top: float = Query(default=0, ge=0, le=1),
                     bottom: float = Query(default=1, ge=0, le=1)):
    if not re.fullmatch(r"[a-f0-9]{32}", pdf_hash) or page_number < 1 or top >= bottom or width not in (960, 1600):
        raise HTTPException(status_code=422, detail="Invalid source image parameters")
    with LineItemStorage() as storage:
        if not storage.database_url:
            raise HTTPException(status_code=503, detail="DATABASE_URL is not configured")
        try:
            local_path = storage.get_digestion_pdf_path(pdf_hash)
        except Exception as error:
            logger.warning("Unable to fetch source PDF path", exc_info=True)
            raise HTTPException(status_code=503, detail="Unable to load source image") from error
    path = source_pdf_path(local_path)
    try:
        image_path = render_page(path, pdf_hash, page_number, SOURCE_CACHE_DIR,
                                 width=width, top=top, bottom=bottom)
    except (OSError, ValueError, RuntimeError) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return FileResponse(image_path, media_type="image/png", headers={
        "Cache-Control": "public, max-age=31536000, immutable",
        "X-Content-Type-Options": "nosniff",
    })


@app.post("/submit/")
def submit_word(data: WordInput):
    """
    API endpoint to receive a word and process it.
    This simulates receiving data from a client (like a web form).
    """
    # The data is automatically validated by Pydantic before this line runs.
    word = data.word_input
    
    logger.info("Server received the word: %s", word)

    # Simulate successful processing
    return {
        "status": "success",
        "message": f"The word '{word}' was successfully received and processed!"
    }

@app.get("/", response_class=HTMLResponse)
def read_root() -> HTMLResponse:
    """Render the public landing page for the three-statement explorer."""
    with LineItemStorage() as storage:
        if not storage.database_url:
            raise HTTPException(status_code=503, detail="DATABASE_URL is not configured")
        try:
            available_stocks = storage.get_statement_stock_codes("balance_sheet")
        except Exception as exc:
            logger.warning("Unable to render public home page", exc_info=True)
            raise HTTPException(status_code=503, detail="Unable to render home page") from exc

    stocks = [
        entry for entry in available_stocks
        if re.fullmatch(r"\d{6}", str(entry.get("stock_code") or ""))
    ]
    stock_directory = {
        str(entry["stock_code"]): str(entry.get("stock_name") or entry["stock_code"])
        for entry in stocks
    }
    stock_directory_json = json.dumps(stock_directory, ensure_ascii=False).replace("<", "\\u003c")
    recommendations = stocks[:12]
    recommendation_links = "".join(
        f'<a class="stock-link" href="/stocks/{escape(str(entry["stock_code"]), quote=True)}/balance-sheet">'
        f'<span>{escape(str(entry.get("stock_name") or entry["stock_code"]))}</span>'
        f'<small>{escape(str(entry["stock_code"]))}</small></a>'
        for entry in recommendations
    )

    return HTMLResponse(f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>ValuePaper · 上市公司财报助手</title>
  <meta name="description" content="快速查看上市公司的资产负债表、利润表和现金流量表，发现更值得研究的企业。">
  <style>
    :root {{ color-scheme: light; }}
    * {{ box-sizing: border-box; }}
    body {{ margin: 0; color: #20283b; background: #f3f5fb; font: 16px system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }}
    a {{ color: inherit; }}
    .home-shell {{ max-width: 1180px; margin: 0 auto; padding: 24px 24px 64px; }}
    .topbar {{ display: flex; align-items: center; justify-content: space-between; gap: 20px; }}
    .brand-line {{ display: flex; align-items: center; gap: 12px; }}
    .brand-icon {{ display: inline-flex; width: 42px; height: 42px; align-items: center; justify-content: center; border-radius: 12px; background: linear-gradient(135deg, #51488e, #8877d0); color: #fff; box-shadow: 0 5px 14px #51488e2b; }}
    .brand-icon svg {{ width: 24px; height: 24px; }}
    .site-logo {{ color: #737c8e; font-size: 1.1rem; font-weight: 700; }}
    .site-logo strong {{ color: #5b51a0; font-size: 1.8rem; letter-spacing: -.04em; }}
    .home-link {{ color: #5d58a7; font-weight: 700; text-decoration: none; }}
    .hero {{ display: grid; grid-template-columns: minmax(0, 1.1fr) minmax(320px, .9fr); align-items: center; gap: 56px; padding: 92px 0 78px; }}
    .eyebrow {{ margin: 0 0 14px; color: #6860ad; font-size: .9rem; font-weight: 800; letter-spacing: .08em; }}
    h1 {{ max-width: 700px; margin: 0; color: #252d45; font-size: clamp(2.4rem, 5vw, 4.4rem); line-height: 1.08; letter-spacing: -.04em; }}
    .hero-copy {{ max-width: 610px; margin: 22px 0 30px; color: #66718a; font-size: 1.15rem; line-height: 1.8; }}
    .search-card {{ padding: 22px; border: 1px solid #d9dfeb; border-radius: 18px; background: #fff; box-shadow: 0 18px 45px #20283b12; }}
    .search-card label {{ display: block; margin-bottom: 10px; color: #343b50; font-size: .9rem; font-weight: 800; }}
    .home-search {{ position: relative; display: flex; gap: 8px; }}
    .home-search input {{ width: 100%; height: 54px; padding: 0 16px; border: 2px solid #c4c0e7; border-radius: 11px; color: #20283b; font: inherit; font-size: 1.15rem; }}
    .home-search input:focus {{ outline: 3px solid #e1def7; border-color: #6860ad; }}
    .home-search > button {{ display: inline-flex; width: 54px; height: 54px; flex: 0 0 54px; align-items: center; justify-content: center; border: 0; border-radius: 11px; background: #6860ad; color: #fff; cursor: pointer; }}
    .home-search > button svg {{ width: 28px; height: 28px; }}
    .home-search > button:hover {{ background: #51488e; }}
    .home-suggestions {{ position: absolute; z-index: 5; top: calc(100% + 8px); left: 0; right: 62px; max-height: 260px; overflow-y: auto; padding: 6px; border: 1px solid #d9dfeb; border-radius: 11px; background: #fff; box-shadow: 0 12px 30px #20283b1c; }}
    .home-suggestions[hidden] {{ display: none; }}
    .home-suggestion {{ display: flex; width: 100%; height: auto; align-items: center; justify-content: space-between; padding: 10px 12px; border: 0; border-radius: 7px; background: transparent; color: #343b50; cursor: pointer; font: inherit; text-align: left; }}
    .home-suggestion:hover, .home-suggestion:focus {{ background: #f0eef9; outline: none; }}
    .home-suggestion small {{ color: #737c8e; }}
    .features {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: 16px; margin-bottom: 54px; }}
    .feature {{ padding: 24px; border: 1px solid #d9dfeb; border-radius: 14px; background: #fff; }}
    .feature-icon {{ color: #6860ad; font-size: 1.7rem; }}
    .feature h2 {{ margin: 12px 0 8px; color: #343b50; font-size: 1.15rem; }}
    .feature p {{ margin: 0; color: #737c8e; line-height: 1.65; }}
    .section-heading {{ display: flex; align-items: baseline; justify-content: space-between; gap: 20px; margin-bottom: 16px; }}
    .section-heading h2 {{ margin: 0; color: #343b50; font-size: 1.5rem; }}
    .section-heading p {{ margin: 0; color: #737c8e; }}
    .stock-grid {{ display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 10px; }}
    .stock-link {{ display: flex; align-items: center; justify-content: space-between; gap: 10px; padding: 13px 14px; border: 1px solid #d9dfeb; border-radius: 10px; background: #fff; color: #4f5fa5; text-decoration: none; }}
    .stock-link:hover {{ border-color: #aaa4df; background: #eeecf9; }}
    .stock-link span {{ overflow: hidden; font-weight: 700; text-overflow: ellipsis; white-space: nowrap; }}
    .stock-link small {{ color: #737c8e; }}
    @media (max-width: 800px) {{ .hero {{ grid-template-columns: 1fr; gap: 28px; padding: 58px 0 52px; }} .features {{ grid-template-columns: 1fr; }} .stock-grid {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }} }}
    @media (max-width: 480px) {{ .home-shell {{ padding-inline: 16px; }} .topbar {{ align-items: flex-start; }} .site-logo strong {{ font-size: 1.45rem; }} .site-logo {{ font-size: .85rem; }} .stock-grid {{ grid-template-columns: 1fr; }} .section-heading {{ display: block; }} .section-heading p {{ margin-top: 6px; }} }}
  </style>
</head>
<body>
  <main class="home-shell">
    <header class="topbar">
      <div class="brand-line">
        <span class="brand-icon" aria-hidden="true"><svg viewBox="0 0 24 24" fill="none"><path d="M5 18V6M5 18h14M8 14l3-4 3 2 4-6" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg></span>
        <div class="site-logo"><strong>ValuePaper</strong> 财报数据助手</div>
      </div>
      <a class="home-link" href="/stocks/300750/balance-sheet">查看示例报告 →</a>
    </header>

    <section class="hero">
      <div>
        <p class="eyebrow">上市公司财报助手</p>
        <h1>读懂企业，<br>从三张报表开始。</h1>
        <p class="hero-copy">快速浏览资产负债表、利润表和现金流量表，跨报告期比较关键数据，让财报研究更清晰、更高效。</p>
      </div>
      <div class="search-card">
        <label for="home-stock-search">搜索股票，开始查看财报</label>
        <form class="home-search" id="home-stock-search-form">
          <input id="home-stock-search" type="search" inputmode="numeric" maxlength="6" autocomplete="off" placeholder="输入股票代码或名称" aria-label="搜索股票代码或名称" aria-controls="home-stock-suggestions" aria-expanded="false" required>
          <button type="submit" aria-label="搜索"><svg viewBox="0 0 24 24" fill="none" aria-hidden="true"><circle cx="10.8" cy="10.8" r="6.3" stroke="currentColor" stroke-width="2"/><path d="m16 16 4.2 4.2" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg></button>
          <div class="home-suggestions" id="home-stock-suggestions" role="listbox" hidden></div>
        </form>
      </div>
    </section>

    <section class="features" aria-label="产品功能">
      <article class="feature"><div class="feature-icon">▤</div><h2>资产负债表</h2><p>了解企业资产结构、负债水平和股东权益，快速判断财务稳健程度。</p></article>
      <article class="feature"><div class="feature-icon">↗</div><h2>利润表</h2><p>追踪营收、利润和盈利能力变化，识别企业成长与经营质量。</p></article>
      <article class="feature"><div class="feature-icon">≈</div><h2>现金流量表</h2><p>观察经营现金流和资金来源，判断利润是否真正转化为现金。</p></article>
    </section>

    <section aria-labelledby="recommended-stocks-title">
      <div class="section-heading"><h2 id="recommended-stocks-title">精选企业</h2><p>从这些企业开始探索</p></div>
      <div class="stock-grid">{recommendation_links}</div>
    </section>
  </main>
  <script>
    const stockDirectory = {stock_directory_json};
    const searchForm = document.getElementById('home-stock-search-form');
    const searchInput = document.getElementById('home-stock-search');
    const suggestions = document.getElementById('home-stock-suggestions');
    function renderSuggestions() {{
      const query = searchInput.value.trim().toLowerCase();
      suggestions.replaceChildren();
      Object.entries(stockDirectory).filter(([code, name]) => !query || code.includes(query) || name.toLowerCase().includes(query)).slice(0, 8).forEach(([code, name]) => {{
        const option = document.createElement('button');
        option.type = 'button';
        option.className = 'home-suggestion';
        option.setAttribute('role', 'option');
        option.innerHTML = `<span>${{name}}</span><small>${{code}}</small>`;
        option.addEventListener('click', () => {{
          searchInput.value = code;
          searchForm.requestSubmit();
        }});
        suggestions.append(option);
      }});
      const open = suggestions.children.length > 0 && document.activeElement === searchInput;
      suggestions.hidden = !open;
      searchInput.setAttribute('aria-expanded', String(open));
    }}
    searchInput.addEventListener('focus', renderSuggestions);
    searchInput.addEventListener('input', renderSuggestions);
    document.addEventListener('click', (event) => {{
      if (!searchForm.contains(event.target)) {{ suggestions.hidden = true; searchInput.setAttribute('aria-expanded', 'false'); }}
    }});
    searchForm.addEventListener('submit', (event) => {{
      event.preventDefault();
      const query = searchInput.value.trim();
      const code = Object.keys(stockDirectory).find((candidate) => candidate === query || stockDirectory[candidate].toLowerCase() === query.toLowerCase());
      if (code) window.location.href = `/stocks/${{encodeURIComponent(code)}}/balance-sheet`;
      else searchInput.setCustomValidity('请输入有效的股票代码或名称');
    }});
  </script>
</body>
</html>""")

@app.get("/health")
def health():
    return {"status": "healthy"}
